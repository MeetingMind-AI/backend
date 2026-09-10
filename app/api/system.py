"""
System Status and Infrastructure Health Diagnostics Module.

Provides real-time cross-platform inspection of backend dependencies including the Ollama AI
inference engine (host vs container differentiation, hardware acceleration,
model readiness, latency), PostgreSQL database, Redis message broker/cache,
Qdrant vector database, and STT transcription services.
"""

from __future__ import annotations

import asyncio
import os
import platform
import time
from typing import Any

from fastapi import APIRouter
import httpx
from sqlalchemy import text

from app.db.session import engine

router = APIRouter(prefix="/api/system", tags=["system"])


def _extract_ollama_base_url() -> str:
    """Resolve the root base URL for Ollama from environment variables.

    Returns:
        str: Base URL (e.g. 'http://host.docker.internal:11434' or 'http://ollama:11434').
    """
    mem0_url = os.getenv("MEM0_OLLAMA_URL", "").strip()
    if mem0_url:
        return mem0_url.rstrip("/")
    gen_url = os.getenv("OLLAMA_URL", "http://ollama:11434/api/generate").strip()
    if "/api" in gen_url:
        return gen_url.split("/api")[0].rstrip("/")
    return gen_url.rstrip("/")


async def _check_ollama(client: httpx.AsyncClient) -> dict[str, Any]:
    """Inspect Ollama connectivity, hardware acceleration, and models.

    Args:
        client (httpx.AsyncClient): Reusable async HTTP client.

    Returns:
        dict[str, Any]: Comprehensive status report for Ollama.
    """
    base_url = _extract_ollama_base_url()
    configured_model = os.getenv("OLLAMA_MODEL", "hermes3:8b").strip()

    # Determine instance deployment type across OS platforms
    is_host = any(h in base_url for h in ("host.docker.internal", "localhost", "127.0.0.1"))
    instance_type = "host" if is_host else ("docker" if "ollama" in base_url else "remote")

    t0 = time.perf_counter()
    try:
        # Check version & latency
        v_resp = await client.get(f"{base_url}/api/version", timeout=3.0)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        version = ""
        if v_resp.is_success:
            version = v_resp.json().get("version", "")

        # Fetch installed models
        available_models: list[str] = []
        model_available = False
        try:
            tags_resp = await client.get(f"{base_url}/api/tags", timeout=3.0)
            if tags_resp.is_success:
                models_data = tags_resp.json().get("models", [])
                available_models = [m.get("name", "") for m in models_data if m.get("name")]
                # Model match (exact or name without tag)
                configured_base = configured_model.split(":")[0]
                model_available = any(
                    configured_model == name or configured_base == name.split(":")[0]
                    for name in available_models
                )
        except Exception:
            pass

        # Fetch actively loaded models in RAM/VRAM
        running_models: list[dict[str, Any]] = []
        has_vram_allocation = False
        try:
            ps_resp = await client.get(f"{base_url}/api/ps", timeout=3.0)
            if ps_resp.is_success:
                raw_models = ps_resp.json().get("models", [])
                for rm in raw_models:
                    vram = rm.get("size_vram", 0)
                    if vram > 0:
                        has_vram_allocation = True
                    running_models.append({
                        "name": rm.get("name", ""),
                        "size": rm.get("size", 0),
                        "size_vram": vram,
                        "expires_at": rm.get("expires_at", ""),
                    })
        except Exception:
            pass

        # Hardware acceleration: host engines typically have GPU (Metal/CUDA),
        # or check explicit VRAM allocation reported by Ollama.
        is_gpu_accelerated = has_vram_allocation or is_host

        if is_host:
            instance_label = "Host Engine (Native)"
        elif instance_type == "docker":
            instance_label = "Docker Container (GPU)" if is_gpu_accelerated else "Docker Container (CPU-only)"
        else:
            instance_label = "Remote Instance"

        return {
            "online": True,
            "instance_type": instance_type,
            "instance_label": instance_label,
            "is_host": is_host,
            "base_url": base_url,
            "version": version,
            "configured_model": configured_model,
            "model_available": model_available,
            "available_models": available_models,
            "running_models": running_models,
            "is_gpu_accelerated": is_gpu_accelerated,
            "latency_ms": latency_ms,
            "error": None,
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        instance_label = "Host Engine (Native)" if is_host else ("Docker Container (CPU-only)" if instance_type == "docker" else "Remote Instance")
        return {
            "online": False,
            "instance_type": instance_type,
            "instance_label": instance_label,
            "is_host": is_host,
            "base_url": base_url,
            "version": None,
            "configured_model": configured_model,
            "model_available": False,
            "available_models": [],
            "running_models": [],
            "is_gpu_accelerated": False,
            "latency_ms": latency_ms,
            "error": str(exc),
        }


def _check_database_sync() -> dict[str, Any]:
    """Inspect PostgreSQL connectivity synchronously."""
    t0 = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"online": True, "latency_ms": latency_ms, "error": None}
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"online": False, "latency_ms": latency_ms, "error": str(exc)}


async def _check_database() -> dict[str, Any]:
    """Asynchronously inspect PostgreSQL database."""
    return await asyncio.to_thread(_check_database_sync)


async def _check_redis() -> dict[str, Any]:
    """Inspect Redis message broker and cache connectivity."""
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0").strip()
    t0 = time.perf_counter()
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(redis_url, socket_timeout=2.0)
        await client.ping()
        await client.aclose()
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"online": True, "latency_ms": latency_ms, "error": None}
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"online": False, "latency_ms": latency_ms, "error": str(exc)}


async def _check_qdrant(client: httpx.AsyncClient) -> dict[str, Any]:
    """Inspect Qdrant vector database connectivity."""
    qdrant_url = os.getenv("MEM0_QDRANT_URL", "http://qdrant:6333").rstrip("/")
    t0 = time.perf_counter()
    try:
        resp = await client.get(f"{qdrant_url}/healthz", timeout=2.0)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"online": resp.is_success, "latency_ms": latency_ms, "error": None}
    except Exception as exc:
        try:
            resp = await client.get(f"{qdrant_url}/", timeout=2.0)
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            return {"online": resp.is_success, "latency_ms": latency_ms, "error": None}
        except Exception:
            pass
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"online": False, "latency_ms": latency_ms, "error": str(exc)}


async def _check_stt(client: httpx.AsyncClient) -> dict[str, Any]:
    """Inspect Vexa STT / transcription service connectivity."""
    stt_url = os.getenv("TRANSCRIPTION_SERVICE_URL", "http://transcription-api:80").rstrip("/")
    t0 = time.perf_counter()
    try:
        resp = await client.get(f"{stt_url}/", timeout=2.0)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"online": resp.status_code < 500, "latency_ms": latency_ms, "error": None}
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"online": False, "latency_ms": latency_ms, "error": str(exc)}


@router.get("/status")
async def get_system_status() -> dict[str, Any]:
    """Retrieve unified real-time status diagnostics for all platform services.

    Returns:
        dict[str, Any]: Live status dictionaries for Ollama, PostgreSQL, Redis,
        Qdrant, and STT transcription services.
    """
    async with httpx.AsyncClient() as client:
        ollama_task = _check_ollama(client)
        db_task = _check_database()
        redis_task = _check_redis()
        qdrant_task = _check_qdrant(client)
        stt_task = _check_stt(client)

        ollama_res, db_res, redis_res, qdrant_res, stt_res = await asyncio.gather(
            ollama_task, db_task, redis_task, qdrant_task, stt_task
        )

    all_critical_online = bool(
        ollama_res.get("online")
        and db_res.get("online")
        and redis_res.get("online")
    )

    return {
        "ok": all_critical_online,
        "ollama": ollama_res,
        "database": db_res,
        "redis": redis_res,
        "qdrant": qdrant_res,
        "stt": stt_res,
        "platform": platform.system(),
        "timestamp": time.time(),
    }
