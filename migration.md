# Draconid Drift — Svelte + AWS Backend Migration

## Core Constraints & Requirements

1. **Pixel-Exact UI Parity**:
   - Keep the UI **exact same** as the Streamlit stage (`web/stage/`), down to the pixel.
   - Preserve exact colors (`--bg: #13161A`, `--bg2: #1a1e23`, `--wine: #7A4A51`, `--wine-hi: #a86a73`, `--good: #74c69d`, `--warn: #e3b04b`, `--bad: #e07a7a`, `--info: #7aa7e0`), typography (`Proxima Nova` / `Montserrat`), spacing, border radii (`--r: 26px`), animations, and layout.
   - Preserve all interactive behaviors:
     - Full-screen Aladin HiPS sky map with spin, scroll-zoom, right-click (both single right-click and right-click-drag) star selection, pixel-peak star centering, and catalog search.
     - Right-click UX flow:
       1. Right-clicking (single click or drag) finds/snaps to the star and immediately begins a **3-second cinematic whooshing-through-the-stars animation** (`flyTo` + squish reveal).
       2. Data processing starts immediately in parallel during the 3-second animation.
       3. A **side-of-screen progress bar** displays live **percent completed** (`0%`–`100%`) while fetching from the EC2 / TAP API backend.
       4. End-to-end loading completes within **4 seconds max** without Streamlit overhead.
     - Star remains visible behind the beacon and progress indicator during processing.
     - Frame player with play/pause, scrubbing, proper-motion trajectory vectors, and hover highlights on "In the frame" objects.
     - Targets, How it works, and Data overlay pages, plus URL deep-linking (`?ra=...&dec=...&fov=...`).

2. **AWS EC2 + S3 Backend Integration**:
   - Connect directly to the AWS EC2 FastAPI backend (`http://127.0.0.1:8000` via SSH tunnel / EC2 endpoint) for S3 range-read SPHEREx cubes (`/api/cube`), spectra (`/api/spectrum`), and health/resolve endpoints.
   - Eliminate Streamlit's `iframe` `postMessage` and script-rerun bottleneck by serving `/api/presets` and `/api/stage` over direct REST JSON endpoints.

3. **Localhost Development via `npm`**:
   - Scaffolded as a Vite + Svelte 5 application in `frontend/`.
   - Run locally on `http://localhost:5173` via `npm run dev` with Vite `/api` proxy routing to the backend.

