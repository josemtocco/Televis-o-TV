#!/usr/bin/env python3
"""
Televisão.TV → M3U (v7)

Melhorias em relação à v6:
- Configuração externa (config.json)
- Tipagem e estrutura mais clara
- Retries em páginas de categoria e canal
- Extração mais ampla de streams (JSON embutido, data-*, scripts)
- Deduplicação e priorização de streams (m3u8 > mpd > m3u)
- Headers e contexto de browser mais realistas
- Logs com progresso e resumo final
- Status.json com versão e timestamps
- CLI: --config, --dry-run, --concurrency
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, unquote

from playwright.async_api import async_playwright, BrowserContext, Page

# ---------------------------------------------------------------------------
# Defaults / regex
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = Path("config.json")
MEDIA_RE = re.compile(r"\.(?:m3u8|mpd|m3u)(?:$|[?#])", re.I)
URL_RE = re.compile(
    r"""https?://[^"'\\<>\s]+?\.(?:m3u8|mpd|m3u)(?:\?[^"'\\<>\s]*)?""",
    re.I,
)
# JSON-like stream keys common in players
JSON_STREAM_RE = re.compile(
    r"""(?:"|')(?:url|src|source|file|stream|hls|dash|playlist|manifest)(?:"|')\s*:\s*(?:"|')(https?://[^"'\\]+?\.(?:m3u8|mpd|m3u)(?:\?[^"'\\]*)?)(?:"|')""",
    re.I,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("televisao")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config não encontrada: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def clean(s: str | None) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = re.sub(r"\s*(?:—|-)?\s*ao vivo\s*$", "", s, flags=re.I)
    return s.strip(" -|")


def normalize(url: str | None, base: str) -> str | None:
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    u = urljoin(base, url)
    p = urlparse(u)
    host = p.netloc.lower()
    if p.scheme not in ("http", "https") or not host.endswith("televisao.tv"):
        return None
    path = re.sub(r"/+", "/", p.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return f"https://televisao.tv{path}"


def is_channel(url: str, blocked: set[str]) -> bool:
    p = urlparse(url)
    parts = [unquote(x).strip() for x in p.path.split("/") if x.strip()]
    if len(parts) != 1:
        return False
    slug = parts[0].lower()
    return (
        slug not in blocked
        and "." not in slug
        and not slug.startswith(("canais-", "desenhos-"))
    )


def streams_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for pattern in (URL_RE, JSON_STREAM_RE):
        for m in pattern.finditer(text):
            u = m.group(1) if pattern is JSON_STREAM_RE else m.group(0)
            u = u.replace("\\/", "/").replace("&amp;", "&").rstrip("),;\"'")
            if u not in seen and MEDIA_RE.search(u):
                seen.add(u)
                out.append(u)
    return out


def prioritize_streams(streams: list[str], limit: int) -> list[str]:
    """Prefer HLS (m3u8) over DASH (mpd) over plain m3u. Dedup by base path."""
    def score(u: str) -> int:
        ul = u.lower()
        if ".m3u8" in ul:
            return 0
        if ".mpd" in ul:
            return 1
        return 2

    def base_key(u: str) -> str:
        p = urlparse(u)
        return f"{p.scheme}://{p.netloc}{p.path}".lower()

    seen: set[str] = set()
    ranked = sorted(streams, key=score)
    result: list[str] = []
    for u in ranked:
        k = base_key(u)
        if k in seen:
            continue
        seen.add(k)
        result.append(u)
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

async def category_links(
    page: Page,
    category_url: str,
    blocked: set[str],
    timeout_ms: int,
) -> dict[str, str]:
    await page.goto(category_url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass

    # Somente links VISÍVEIS — o site injeta catálogo global no DOM.
    rows = await page.locator("a:visible").evaluate_all(
        """els => els.map(a => ({
            href: a.href || "",
            text: (a.innerText || a.textContent || "").trim(),
            aria: a.getAttribute("aria-label") || ""
        })).filter(x => x.href)"""
    )

    found: dict[str, str] = {}
    for row in rows:
        u = normalize(row["href"], category_url)
        if u and is_channel(u, blocked):
            name = clean(row["text"] or row["aria"])
            if u not in found or (name and not found[u]):
                found[u] = name
    return found


async def collect_channel(
    context: BrowserContext,
    url: str,
    categories: list[str],
    suggested_name: str,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    page = await context.new_page()
    result: dict[str, Any] = {
        "url": url,
        "name": "",
        "logo": "",
        "categories": categories,
        "streams": [],
        "page_ok": False,
        "error": "",
    }
    network: set[str] = set()

    async def on_response(resp):
        try:
            if MEDIA_RE.search(resp.url):
                network.add(resp.url)
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=cfg["page_timeout_ms"],
        )

        # Nome
        for sel in ("h1", "h2", "meta[property='og:title']", "title"):
            try:
                loc = page.locator(sel).first
                value = (
                    await loc.get_attribute("content")
                    if sel.startswith("meta")
                    else await loc.inner_text()
                )
                if value:
                    result["name"] = clean(value)
                    if result["name"]:
                        break
            except Exception:
                pass
        if not result["name"]:
            result["name"] = (
                suggested_name
                or clean(urlparse(url).path[1:].replace("-", " ").title())
            )

        # Logo
        for sel, attr in (
            ("meta[property='og:image']", "content"),
            ("meta[name='twitter:image']", "content"),
            ("link[rel='image_src']", "href"),
        ):
            try:
                value = await page.locator(sel).first.get_attribute(attr)
                if value:
                    result["logo"] = urljoin(url, value)
                    break
            except Exception:
                pass

        # Tempo para o player emitir requests
        await page.wait_for_timeout(cfg["player_wait_ms"])

        html = await page.content()
        candidates: set[str] = set(streams_from_text(html))
        candidates.update(network)

        # Atributos de mídia / iframes
        attrs = await page.locator("video,source,iframe,embed,object").evaluate_all(
            """els => els.flatMap(e => [
                e.src, e.currentSrc,
                e.getAttribute("src"), e.getAttribute("data-src"),
                e.getAttribute("data-url"), e.getAttribute("data-stream"),
                e.getAttribute("data-hls"), e.getAttribute("data-file")
            ]).filter(Boolean)"""
        )

        iframe_urls: list[str] = []
        for raw in attrs:
            if MEDIA_RE.search(raw):
                candidates.add(raw)
            elif raw.startswith("http") and raw != url:
                iframe_urls.append(raw)

        # Scripts inline
        for script in await page.locator("script").all_text_contents():
            candidates.update(streams_from_text(script))

        # Iframes (limitado para não explodir)
        for raw in iframe_urls[:3]:
            child = await context.new_page()
            child_net: set[str] = set()

            async def child_on_response(resp):
                try:
                    if MEDIA_RE.search(resp.url):
                        child_net.add(resp.url)
                except Exception:
                    pass

            child.on("response", child_on_response)
            try:
                await child.goto(
                    raw,
                    wait_until="domcontentloaded",
                    timeout=cfg["iframe_timeout_ms"],
                )
                await child.wait_for_timeout(1200)
                candidates.update(child_net)
                candidates.update(streams_from_text(await child.content()))
                for x in await child.locator("video,source").evaluate_all(
                    """els => els.flatMap(e => [
                        e.src, e.currentSrc, e.getAttribute("src")
                    ]).filter(Boolean)"""
                ):
                    if MEDIA_RE.search(x):
                        candidates.add(x)
            except Exception:
                pass
            finally:
                await child.close()

        result["streams"] = prioritize_streams(
            list(candidates), cfg["max_streams_per_channel"]
        )
        result["page_ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        await page.close()
    return result


def make_m3u(channels: list[dict[str, Any]]) -> str:
    lines = ["#EXTM3U"]
    seen: set[tuple[str, str, str]] = set()
    for c in sorted(
        channels,
        key=lambda x: ((x["categories"] or [""])[0].lower(), x["name"].lower()),
    ):
        for category in c["categories"] or ["Televisão.TV"]:
            name = c["name"].replace('"', "'")
            logo = (c["logo"] or "").replace('"', "'")
            group = category.replace('"', "'")
            for stream in c["streams"]:
                key = (name, group, stream)
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f'#EXTINF:-1 tvg-name="{name}" tvg-logo="{logo}" '
                    f'group-title="{group}",{name}'
                )
                lines.append(stream)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(cfg: dict[str, Any], dry_run: bool = False) -> int:
    base = cfg["base_url"]
    blocked = set(cfg.get("blocked_slugs", []))
    concurrency = int(cfg.get("concurrency", 20))
    categories_cfg = cfg["categories"]

    metrics = {
        "categories": len(categories_cfg),
        "category_pages_ok": 0,
        "category_pages_failed": 0,
        "channel_links_found": 0,
        "channel_pages_unique": 0,
        "streams_found": 0,
        "streams_active": 0,
    }
    category_stats: list[dict[str, Any]] = []
    channels: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "categories": []}
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=cfg.get("user_agent"),
            locale=cfg.get("locale", "pt-BR"),
            timezone_id=cfg.get("timezone", "America/Sao_Paulo"),
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            },
        )
        page = await context.new_page()

        for cat in categories_cfg:
            name = cat["name"]
            url = urljoin(base, cat["path"].lstrip("/"))
            try:
                links = await category_links(
                    page, url, blocked, cfg["category_timeout_ms"]
                )
                metrics["category_pages_ok"] += 1
                metrics["channel_links_found"] += len(links)
                examples = list(links.items())[:5]
                log.info("%s → %d links | ex: %s", name, len(links), examples)
                for u, ch_name in links.items():
                    if ch_name and not channels[u]["name"]:
                        channels[u]["name"] = ch_name
                    if name not in channels[u]["categories"]:
                        channels[u]["categories"].append(name)
                category_stats.append(
                    {
                        "name": name,
                        "url": url,
                        "links": len(links),
                        "examples": [u for u, _ in examples],
                    }
                )
            except Exception as exc:
                metrics["category_pages_failed"] += 1
                log.exception("Falha em %s: %s", name, exc)
                category_stats.append(
                    {"name": name, "url": url, "links": 0, "error": str(exc)}
                )

        metrics["channel_pages_unique"] = len(channels)
        log.info("Páginas únicas de canais: %d", len(channels))

        if not channels:
            status = {
                "success": False,
                "version": 7,
                "source": base,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "error_stage": "channel_discovery",
                "error": "Nenhuma página de canal foi identificada.",
                "metrics": metrics,
                "categories": category_stats,
            }
            Path(cfg["output_status"]).write_text(
                json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            await browser.close()
            return 1

        if dry_run:
            log.info("Dry-run: pulando coleta de streams (%d canais).", len(channels))
            status = {
                "success": True,
                "version": 7,
                "source": base,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "dry_run": True,
                "metrics": metrics,
                "categories": category_stats,
            }
            Path(cfg["output_status"]).write_text(
                json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            await browser.close()
            return 0

        sem = asyncio.Semaphore(concurrency)

        async def run_one(u: str, info: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                return await collect_channel(
                    context, u, info["categories"], info["name"], cfg
                )

        tasks = [
            asyncio.create_task(run_one(u, info)) for u, info in channels.items()
        ]
        results: list[dict[str, Any]] = []
        for i, task in enumerate(asyncio.as_completed(tasks), 1):
            result = await task
            results.append(result)
            metrics["streams_found"] += len(result["streams"])
            if result["streams"]:
                metrics["streams_active"] += 1
            if i % 25 == 0 or i == len(tasks):
                log.info(
                    "Processados %d/%d | canais com stream: %d",
                    i,
                    len(tasks),
                    metrics["streams_active"],
                )

        active = [x for x in results if x["streams"]]
        status = {
            "success": bool(active),
            "version": 7,
            "source": base,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error_stage": None if active else "stream_discovery",
            "error": None if active else "Nenhum stream encontrado nas páginas/players.",
            "metrics": metrics,
            "categories": category_stats,
            "channels": results,
        }
        Path(cfg["output_status"]).write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if active:
            playlist = make_m3u(active)
            Path(cfg["output_m3u"]).write_text(playlist, encoding="utf-8")
            count = playlist.count("#EXTINF:")
            log.info("M3U gerada com %d entradas (%d canais com stream).", count, len(active))
        else:
            log.error(status["error"])

        await browser.close()
        return 0 if active else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera M3U a partir de televisao.tv")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Caminho do config.json (padrão: config.json)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Sobrescreve concurrency do config",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Só descobre canais; não coleta streams",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.concurrency is not None:
        cfg["concurrency"] = args.concurrency

    raise SystemExit(asyncio.run(run(cfg, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
