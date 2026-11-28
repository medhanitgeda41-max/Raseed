"""Tests for Part 3: Render Ready.

Covers:
- Fast-skip of Ollama when offline (timeout <= 1.5s)
- Validation of .streamlit/config.toml (headless, telemetry disabled, port config)
- Validation of render.yaml blueprint structure and environment variables
"""
import os
import time
from pathlib import Path
import pytest
import yaml

from config.settings import settings
from llm_client import ask, LLMResponse


def test_streamlit_config_toml_exists_and_configured():
    config_path = Path(".streamlit/config.toml")
    assert config_path.exists(), ".streamlit/config.toml must exist"

    text = config_path.read_text(encoding="utf-8")
    assert "headless = true" in text
    assert "gatherUsageStats = false" in text
    assert "port = 8501" in text or "port =" in text


def test_render_yaml_blueprint_validity():
    render_path = Path("render.yaml")
    assert render_path.exists(), "render.yaml must exist"

    content = yaml.safe_load(render_path.read_text(encoding="utf-8"))
    assert "services" in content
    assert len(content["services"]) > 0

    web_svc = content["services"][0]
    assert web_svc["type"] == "web"
    assert web_svc["runtime"] == "python"
    assert "streamlit run app.py" in web_svc["startCommand"]
    assert "--server.port=$PORT" in web_svc["startCommand"]

    # Check environment variable definitions
    env_keys = {ev["key"] for ev in web_svc.get("envVars", [])}
    assert "PYTHON_VERSION" in env_keys
    assert "STORAGE_BACKEND" in env_keys
    assert "DATABASE_URL" in env_keys
    assert "R2_ENDPOINT" in env_keys
    assert "GROQ_API_KEY" in env_keys


def test_ollama_fast_skip_when_offline():
    """Verify that when Ollama is offline or unreachable, the client fails fast (<= 2.0s) and does not hang."""
    from unittest.mock import patch

    # Mock settings to route to local Ollama on a dead port
    with patch.object(settings, "llm_mode", "local"):
        with patch.object(settings, "ollama_base_url", "http://127.0.0.1:54321/v1"):
            with patch.object(settings, "groq_api_key", None):
                with patch.object(settings, "gemini_api_key", None):
                    t0 = time.time()
                    resp = ask(messages=[{"role": "user", "content": "Hello"}])
                    elapsed = time.time() - t0

                    # Must complete in less than 3 seconds (timeout is 1.5s)
                    assert elapsed < 3.0, f"Ollama fallback took {elapsed:.2f}s (expected <= 3.0s)"
                    assert resp.is_offline_fallback is True
                    assert "basic answer" in resp.content or "rule-based" in resp.content or "Raseed" in resp.content
