<div align="center">
  <img src="./assets/icon.svg" width="100" alt="Folium" />
  <h1>Folium</h1>
  <p><strong>Self-hosted EPUB translation, powered by a local LLM</strong></p>
  <p>
    A tiny FastAPI wrapper around <a href="https://github.com/oomol-lab/epub-translator">epub-translator</a><br/>
    plus a cross-platform Avalonia desktop client, fully dockerized alongside Ollama running Qwen3 locally.
  </p>
</div>

---

## System Requirements

- Docker and Docker Compose
- (Desktop client) [.NET 10 SDK](https://dotnet.microsoft.com/download/dotnet/10.0) — runs on Windows, Linux and macOS via Avalonia
- A GPU is recommended for Ollama (the bundled compose targets AMD/ROCm)

## Getting Started

```bash
cp folium-api/.env.example folium-api/.env   # optional, only for local (non-compose) runs
docker compose up -d
```

| Service | URL |
|---------|-----|
| API | http://localhost:8000 |
| Ollama | http://localhost:11434 |

The `ollama` service is pulled with the `rocm` image tag and mounts `/dev/kfd` and `/dev/dri`, targeting AMD GPUs (`HSA_OVERRIDE_GFX_VERSION=10.3.0`). Adjust the image/devices in `docker-compose.yml` if you're on NVIDIA/CPU.

On startup, the `ollama` container automatically pulls `qwen2.5:3b-instruct-q4_K_M` (a small, quantized model sized to fit a few parallel `JOB_WORKERS` on an 8GB GPU) once its server is ready — no manual step needed. Change the tag in `docker-compose.yml` (and `OLLAMA_MODEL` in the `api` service) if you want a different model size/quantization.

### Running the API locally (development)

```bash
# Start Ollama only
docker compose up ollama -d

cd folium-api
pip install -r requirements.txt
export OLLAMA_BASE_URL=http://localhost:11434/v1
uvicorn app.main:app --reload
```

### Running the desktop client

```bash
cd FoliumUi
dotnet run
```

Point the app at the API URL (default `http://localhost:8000`) on the upload screen.

## Building

```bash
docker build -f folium-api/dockerfile -t Folium-api .
```

```bash
dotnet build FoliumUi/FoliumUi.csproj
```

## Architecture

<div align="center" style="margin-bottom:50px">
  <br/>
  <img src="assets/diagram.svg" alt="architecture diagram" />
  <br/>
</div>

| Component | Type | Responsibility |
|-----------|------|----------------|
| `folium-api` | FastAPI service | Accepts EPUB uploads, runs `epub-translator` jobs against Ollama, exposes progress + download |
| `FoliumUi` | Avalonia desktop app (.NET 10) | Pick an `.epub`, configure target language/prompt, track progress, download the translated file — runs on Windows, Linux and macOS |
| `ollama` | Ollama container | Serves the local LLM (Qwen3 by default) via an OpenAI-compatible endpoint |

Translation jobs run in-process on a thread pool (`JOB_WORKERS`) inside the API container — there's no external queue. Job state (status, progress, last warning) is kept in memory and polled/streamed by the client.

## API

### Translations

| Method | Route | Description |
|--------|-------|--------------|
| `POST` | `/translations` | Upload an `.epub` and start a translation job |
| `GET` | `/translations/{job_id}` | Get job status, progress and last warning |
| `GET` | `/translations/{job_id}/events` | Server-Sent Events stream of job status until `completed`/`failed` |
| `GET` | `/translations/{job_id}/download` | Download the translated `.epub` once the job is `completed` |

`POST /translations` accepts a multipart form:

| Field | Required | Description |
|-------|----------|-------------|
| `file` | yes | `.epub` file to translate |
| `target_language` | yes | Target language (e.g. `pt-BR`, `en`) |
| `concurrency` | no | Parallel translation requests to the LLM (default `1`) |
| `user_prompt` | no | Extra instructions appended to the translation prompt |
| `submit_kind` | no | `REPLACE`, `APPEND_TEXT` or `APPEND_BLOCK` (default `APPEND_BLOCK`) — how the translation is merged into the original text |

## Configuration

`folium-api/.env.example`:

```env
LLM_PROVIDER=openai
API_KEY=
MAX_GROUP_TOKENS=1000
TOKEN_ENCODING=cl100k_base
STORAGE_DIR=./data
JOB_WORKERS=4
```

Provider selection is a small **adapter factory** in `config.py`: each provider (`ollama`, `deepseek`, `openai`) is a tiny class holding its own defaults (`default_base_url`, `default_model`, plus provider-specific extras like Ollama's `num_ctx`), registered in a `_PROVIDERS` dict. The env var surface is the same **`API_KEY` / `BASE_URL` / `MODEL`** trio no matter which provider is selected — only set `BASE_URL`/`MODEL` if you want to override that provider's default. Adding another OpenAI-compatible provider is one adapter class, no new env var names.

> **Note:** Claude/Anthropic is not a supported `LLM_PROVIDER`. The `epub-translator` library's `LLM` client is hardcoded to the OpenAI chat-completions wire format, and Anthropic's Messages API has no official OpenAI-compatible endpoint — so only genuinely OpenAI-compatible backends (Ollama, DeepSeek, OpenAI itself) can be plugged in this way.

| Variable | Description |
|----------|--------------|
| `LLM_PROVIDER` | `openai` (default), `deepseek`, or `ollama` (local) — selects which adapter resolves `API_KEY`/`BASE_URL`/`MODEL` |
| `API_KEY` | API key for the selected provider. Required for `openai`/`deepseek`; for `ollama` it can be any value (the server ignores it) |
| `BASE_URL` | Overrides the provider's default endpoint (e.g. `http://localhost:11434/v1` for a local Ollama, `https://api.openai.com/v1` for OpenAI) |
| `MODEL` | Overrides the provider's default model |
| `OLLAMA_NUM_CTX` | Ollama-only: context window (tokens) requested per call, kept below the model's native window (default `30000`) |
| `MAX_GROUP_TOKENS` | Max source tokens per translation/fill group; smaller groups mean fewer block elements to align per request, which helps smaller/local models preserve structure. Stronger cloud models (OpenAI, DeepSeek) tolerate larger values (default `1000`) |
| `TOKEN_ENCODING` | `tiktoken` encoding used to estimate tokens when batching segments |
| `STORAGE_DIR` | Where uploaded/translated EPUBs are stored |
| `JOB_WORKERS` | Max number of translation jobs running in parallel. With a cloud provider this becomes bounded by the account's rate limits rather than local VRAM |

## Tech Stack

| Layer | Technology |
|-------|------------|
| API | FastAPI + Uvicorn |
| Translation engine | [epub-translator](https://github.com/oomol-lab/epub-translator) |
| LLM runtime | Ollama (Qwen3 0.6B Q4_K_M by default, ROCm image) |
| Desktop client | Avalonia UI (.NET 10, cross-platform) |
| Packaging | Docker / Docker Compose |
