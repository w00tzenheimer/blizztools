"""FastAPI-based local web UI for blizztools."""

from __future__ import annotations

import asyncio
import threading
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from blizztools.main import (
    BASE_URL,
    USE_HTTP2,
    build_ckey_lookup,
    download_command,
    fetch,
    filter_install_entries,
    find_existing_file_by_path,
    get_ckey_for_file_path,
    install_manifest_command,
    is_file_already_downloaded,
    load_ckey_map,
    make_unique_filename,
    normalize_region_group,
    pick_version_for_region,
    save_ckey_map,
    update_ckey_map,
)
from blizztools.parsers import parse_cdn_table, parse_version_table
from blizztools.products import (
    ALL_PRODUCT_CODES,
    Product,
    _code_to_cli_name,
    product_name_to_enum,
)
from blizztools.tags import build_variant_names

STATIC_DIR = Path(__file__).resolve().parent / "static"

APP_VERSION = "0.1.1"
try:
    from importlib.metadata import version as _pkg_version

    APP_VERSION = _pkg_version("blizztools")
except Exception:
    pass

WAGO_BUILDS_URL = "https://wago.tools/api/builds"
_WAGO_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "data": {}}
_WAGO_TTL_SECONDS = 300.0

FAVORITE_PRODUCTS = [
    "wow",
    "wow-beta",
    "wow-classic",
    "wow-classic-beta",
    "wow-classic-era",
    "wow-classic-ptr",
    "warcraft3",
    "diablo4",
    "overwatch",
    "hearthstone",
]


class ManifestRequest(BaseModel):
    product: str
    region: str = "GL"
    build_config: Optional[str] = None
    version_name: Optional[str] = None
    cdn_config: Optional[str] = None


class DownloadRequest(BaseModel):
    product: str
    dest_dir: str = "./target"
    overwrite: bool = False
    region: str = "GL"
    build_config: Optional[str] = None
    version_name: Optional[str] = None
    cdn_config: Optional[str] = None
    ckeys: List[str] = Field(default_factory=list)
    pattern: Optional[str] = None


@dataclass
class JobState:
    id: str
    status: str = "queued"
    product: str = ""
    dest_dir: str = ""
    region_group: str = "GL"
    region: str = ""
    total: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    current: str = ""
    logs: List[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    finished_at: Optional[str] = None

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > 500:
            self.logs = self.logs[-500:]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "product": self.product,
            "dest_dir": self.dest_dir,
            "region_group": self.region_group,
            "region": self.region,
            "total": self.total,
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
            "current": self.current,
            "logs": self.logs[-100:],
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


JOBS: Dict[str, JobState] = {}
JOBS_LOCK = asyncio.Lock()


async def _fetch_wago_builds() -> Dict[str, Any]:
    now = time.time()
    cached = _WAGO_CACHE.get("data") or {}
    if cached and (now - float(_WAGO_CACHE.get("fetched_at") or 0)) < _WAGO_TTL_SECONDS:
        return cached
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(WAGO_BUILDS_URL)
            response.raise_for_status()
            data = response.json()
        if isinstance(data, dict):
            _WAGO_CACHE["data"] = data
            _WAGO_CACHE["fetched_at"] = now
            return data
    except Exception:
        if cached:
            return cached
        return {}
    return {}


def _cli_name_for_enum(product_enum: Product) -> str:
    for code in ALL_PRODUCT_CODES:
        name = _code_to_cli_name(code)
        if product_name_to_enum(name) == product_enum:
            return name
    return product_enum.value.replace("_", "-")


def _cli_products() -> List[Dict[str, Any]]:
    favorites = []
    seen = set()
    for name in FAVORITE_PRODUCTS:
        enum = product_name_to_enum(name)
        if not enum:
            continue
        favorites.append(
            {
                "id": name,
                "label": name,
                "cdn_code": enum.value,
                "enum_name": enum.name,
                "favorite": True,
            }
        )
        seen.add(name)

    others = []
    for code in ALL_PRODUCT_CODES:
        name = _code_to_cli_name(code)
        if name in seen:
            continue
        enum = product_name_to_enum(name)
        if not enum:
            continue
        others.append(
            {
                "id": name,
                "label": name,
                "cdn_code": enum.value,
                "enum_name": enum.name,
                "favorite": False,
            }
        )
    others.sort(key=lambda item: item["id"])
    return favorites + others


def _resolve_product(product: str) -> Product:
    enum = product_name_to_enum(product)
    if enum:
        return enum
    try:
        return Product[product]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown product: {product}") from exc


def create_app() -> FastAPI:
    app = FastAPI(title="blizztools UI", version=APP_VERSION)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/api/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/api/products")
    async def products() -> Dict[str, Any]:
        items = _cli_products()
        return {
            "favorites": [p for p in items if p["favorite"]],
            "all": items,
        }

    @app.get("/api/versions/{product}")
    async def versions(product: str) -> Dict[str, Any]:
        product_enum = _resolve_product(product)
        url = f"{BASE_URL}/{product_enum.value}/versions"
        async with httpx.AsyncClient(timeout=30.0) as client:
            text = await fetch(url, client)
        table = parse_version_table(text)
        return {
            "product": product_enum.value,
            "versions": [
                {
                    "region": item.region,
                    "build_config": str(item.build_config),
                    "cdn_config": str(item.cdn_config),
                    "build_id": item.build_id,
                    "version_name": item.version_name,
                }
                for item in table
            ],
        }

    @app.get("/api/builds/{product}")
    async def builds(product: str, region: str = "GL") -> Dict[str, Any]:
        product_enum = _resolve_product(product)
        region_group = normalize_region_group(region)
        url = f"{BASE_URL}/{product_enum.value}/versions"
        items: List[Dict[str, Any]] = []
        seen = set()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                text = await fetch(url, client)
            table = parse_version_table(text)
            live, selected_region = pick_version_for_region(table, region_group)
            items.append(
                {
                    "version": live.version_name,
                    "build_id": live.build_id,
                    "build_config": str(live.build_config),
                    "cdn_config": str(live.cdn_config),
                    "region": selected_region,
                    "source": "live",
                    "is_live": True,
                }
            )
            seen.add(str(live.build_config).lower())
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Failed to load live versions: {exc}"
            ) from exc

        wago = await _fetch_wago_builds()
        historical = wago.get(product_enum.value) or []
        for row in historical:
            bc = str(row.get("build_config") or "").lower()
            if not bc or bc in seen:
                continue
            seen.add(bc)
            items.append(
                {
                    "version": row.get("version") or bc,
                    "build_id": None,
                    "build_config": bc,
                    "cdn_config": row.get("cdn_config"),
                    "region": None,
                    "source": "wago",
                    "is_live": False,
                    "created_at": row.get("created_at"),
                }
            )

        return {
            "product": product_enum.value,
            "region_group": region_group,
            "count": len(items),
            "builds": items,
            "note": (
                "Official CDN /versions only has the current build. "
                "Historical entries come from wago.tools; old data may require archive mirrors."
            ),
        }

    @app.get("/api/cdns/{product}")
    async def cdns(product: str) -> Dict[str, Any]:
        product_enum = _resolve_product(product)
        url = f"{BASE_URL}/{product_enum.value}/cdns"
        async with httpx.AsyncClient(timeout=30.0) as client:
            text = await fetch(url, client)
        table = parse_cdn_table(text)
        return {
            "product": product_enum.value,
            "cdns": [
                {
                    "name": item.name,
                    "path": item.path,
                    "hosts": list(item.hosts),
                    "servers": list(item.servers),
                }
                for item in table
            ],
        }

    @app.post("/api/manifest")
    async def manifest(req: ManifestRequest) -> Dict[str, Any]:
        product_enum = _resolve_product(req.product)
        try:
            (
                install_manifest,
                version_name,
                pool,
                _resolver,
                _encoding_ekey,
                selected_region,
                region_group,
                build_config_hash,
                _cdn_config_hash,
            ) = await install_manifest_command(
                product_enum.name,
                None,
                None,
                return_data=True,
                region=req.region,
                build_config=req.build_config,
                version_name=req.version_name,
                cdn_config=req.cdn_config,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        filtered = filter_install_entries(install_manifest, region_group=region_group)
        variant_names = build_variant_names(
            install_manifest.entries,
            install_manifest.tags,
            install_manifest.num_entries,
        )

        entries = []
        for index, entry in filtered:
            raw_name = entry.name or ""
            name = variant_names.get(index, raw_name).replace("\\", "/")
            if name.startswith("ÿ"):
                name = name[1:]
            entries.append(
                {
                    "name": name,
                    "raw_name": raw_name.replace("\\", "/"),
                    "ckey": str(entry.hash),
                    "size": int(entry.size),
                    "index": index,
                }
            )

        seen = set()
        unique_entries = []
        for item in entries:
            key = (item["ckey"], item["name"])
            if key in seen:
                continue
            seen.add(key)
            unique_entries.append(item)

        cli_name = _cli_name_for_enum(product_enum)
        return {
            "product": cli_name,
            "enum_name": product_enum.name,
            "cdn_code": product_enum.value,
            "version": version_name,
            "build_config": build_config_hash,
            "cdn": getattr(pool, "primary", ""),
            "region": selected_region,
            "region_group": region_group,
            "count": len(unique_entries),
            "entries": unique_entries,
        }

    @app.get("/api/ckey-map")
    async def ckey_map(dest_dir: str = "./target") -> Dict[str, Any]:
        dest_path = Path(dest_dir).expanduser().resolve()
        mapping = load_ckey_map(dest_path)
        return {"dest_dir": str(dest_path), "count": len(mapping), "map": mapping}

    @app.post("/api/download")
    async def start_download(req: DownloadRequest) -> Dict[str, Any]:
        product_enum = _resolve_product(req.product)
        cli_name = _cli_name_for_enum(product_enum)

        if not req.ckeys and not req.pattern:
            raise HTTPException(
                status_code=400, detail="Provide ckeys and/or a pattern to download"
            )

        job_id = uuid.uuid4().hex[:12]
        region_group = normalize_region_group(req.region)
        job = JobState(
            id=job_id,
            product=cli_name,
            dest_dir=req.dest_dir,
            region_group=region_group,
        )
        async with JOBS_LOCK:
            JOBS[job_id] = job

        thread = threading.Thread(
            target=_run_download_job_thread,
            kwargs={
                "job": job,
                "product_enum": product_enum,
                "cli_name": cli_name,
                "dest_dir": req.dest_dir,
                "overwrite": req.overwrite,
                "ckeys": set(req.ckeys),
                "pattern": req.pattern,
                "region": req.region,
                "build_config": req.build_config,
                "version_name": req.version_name,
                "cdn_config": req.cdn_config,
            },
            daemon=True,
        )
        thread.start()
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str) -> Dict[str, Any]:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.to_dict()

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


def _run_download_job_thread(**kwargs) -> None:
    asyncio.run(_run_download_job(**kwargs))


async def _run_download_job(
    job: JobState,
    *,
    product_enum: Product,
    cli_name: str,
    dest_dir: str,
    overwrite: bool,
    ckeys: Set[str],
    pattern: Optional[str],
    region: str = "GL",
    build_config: Optional[str] = None,
    version_name: Optional[str] = None,
    cdn_config: Optional[str] = None,
) -> None:
    job.status = "running"
    job.log(f"Loading install manifest for {cli_name}")

    try:
        region_group = normalize_region_group(region)
        job.region_group = region_group
        (
            install_manifest,
            version_name,
            pool,
            resolver,
            encoding_ekey,
            selected_region,
            region_group,
            build_config_hash,
            cdn_config_hash,
        ) = await install_manifest_command(
            product_enum.name,
            None,
            None,
            return_data=True,
            region=region_group,
            build_config=build_config,
            version_name=version_name,
            cdn_config=cdn_config,
        )
        job.region = selected_region
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        job.log(f"Failed to load manifest: {exc}")
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return

    compiled = re.compile(pattern, re.IGNORECASE) if pattern else None
    filtered = filter_install_entries(install_manifest, region_group=region_group)
    variant_names = build_variant_names(
        install_manifest.entries,
        install_manifest.tags,
        install_manifest.num_entries,
    )

    selected = []
    for index, entry in filtered:
        raw_name = entry.name or ""
        display_name = variant_names.get(index, raw_name).replace("\\", "/")
        if display_name.startswith("ÿ"):
            display_name = display_name[1:]
        ckey = str(entry.hash)
        matched = False
        if ckeys and ckey in ckeys:
            matched = True
        if compiled and compiled.search(display_name):
            matched = True
        if matched:
            selected.append((display_name, ckey, raw_name, index))

    dedup = {}
    for name, ckey, raw_name, index in selected:
        dedup[ckey] = (name, raw_name, index)
    selected_items = [
        (name, ckey, raw_name, index)
        for ckey, (name, raw_name, index) in dedup.items()
    ]

    job.total = len(selected_items)
    job.log(
        f"Region {region_group}/{selected_region} version {version_name} via {getattr(pool, 'primary', '')}"
    )
    job.log(f"Matched {job.total} file(s)")

    if job.total == 0:
        job.status = "completed"
        job.log("Nothing to download")
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return

    dest_path = Path(dest_dir).expanduser().resolve()
    dest_path.mkdir(parents=True, exist_ok=True)
    ckey_map = load_ckey_map(dest_path)

    wanted = set()
    entry_by_ckey = {str(e.hash): e for _, e in filtered}
    for _, ckey, _, _ in selected_items:
        entry = entry_by_ckey.get(ckey)
        if entry is not None:
            wanted.add(entry.hash.data)

    ckey_lookup = {}
    if wanted:
        try:
            async with httpx.AsyncClient(http2=USE_HTTP2) as enc_client:
                ckey_lookup = await build_ckey_lookup(
                    pool, encoding_ekey, resolver, wanted, enc_client
                )
            job.log(f"Resolved {len(ckey_lookup)}/{len(wanted)} CKey->EKey mappings")
            resolver.set_wanted(set(ckey_lookup.values()))
        except Exception as exc:
            job.log(f"Warning: encoding lookup failed, falling back per-file: {exc}")
            ckey_lookup = None

    for name, ckey, raw_name, _index in selected_items:
        job.current = name

        existing = is_file_already_downloaded(dest_path, ckey, ckey_map)
        if existing and not overwrite:
            job.skipped += 1
            job.log(f"Skip {name} (already exists)")
            continue

        path_existing = find_existing_file_by_path(
            dest_path, f"{cli_name}/{region_group.lower()}", version_name, raw_name
        )
        if path_existing and not overwrite:
            existing_ckey = get_ckey_for_file_path(dest_path, path_existing, ckey_map)
            if existing_ckey == ckey:
                job.skipped += 1
                job.log(f"Skip {name} (path exists)")
                continue

        try:
            downloaded_path = await download_command(
                product_enum.name,
                ckey,
                str(dest_path),
                None,
                None,
                version_name=version_name,
                pool=pool,
                return_path=True,
                resolver=resolver,
                ckey_lookup=ckey_lookup,
                build_config=build_config_hash,
                cdn_config=cdn_config_hash,
                region=region_group,
            )
            if not downloaded_path:
                job.failed += 1
                job.log(f"Fail {name}: download returned empty")
                continue

            downloaded_path_obj = Path(downloaded_path)
            if not downloaded_path_obj.exists():
                job.failed += 1
                job.log(f"Fail {name}: temp file missing")
                continue

            target_dir = dest_path / cli_name / region_group.lower() / version_name
            target_dir.mkdir(parents=True, exist_ok=True)
            normalized_name = raw_name.replace("\\", "/")
            path_parts = normalized_name.split("/")
            if len(path_parts) > 1:
                file_dir = target_dir
                for part in path_parts[:-1]:
                    file_dir = file_dir / part
                file_dir.mkdir(parents=True, exist_ok=True)
                base_filename = file_dir / path_parts[-1]
            else:
                base_filename = target_dir / path_parts[0]
                base_filename.parent.mkdir(parents=True, exist_ok=True)

            if overwrite and base_filename.exists():
                proper_filename = base_filename
            else:
                proper_filename = make_unique_filename(base_filename, ckey)

            if proper_filename.exists():
                proper_filename.unlink()
            downloaded_path_obj.rename(proper_filename)

            update_ckey_map(
                dest_path,
                ckey,
                proper_filename,
                cli_name,
                version_name,
                ckey_map,
            )
            save_ckey_map(dest_path, ckey_map)

            job.completed += 1
            rel = proper_filename.relative_to(dest_path)
            job.log(f"OK {name} -> {rel}")
        except Exception as exc:
            job.failed += 1
            job.log(f"Fail {name}: {exc}")

    job.current = ""
    job.status = "completed" if job.failed == 0 else "completed_with_errors"
    job.log(
        f"Done. downloaded={job.completed} skipped={job.skipped} failed={job.failed}"
    )
    job.finished_at = datetime.now(timezone.utc).isoformat()


def run_ui(host: str = "127.0.0.1", port: int = 9870, open_browser: bool = True) -> None:
    if open_browser:
        import webbrowser

        def _open() -> None:
            webbrowser.open(f"http://{host}:{port}")

        threading.Timer(1.0, _open).start()

    uvicorn.run(
        "blizztools.webui.app:create_app",
        factory=True,
        host=host,
        port=port,
        log_level="info",
    )


app = create_app()
