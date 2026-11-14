# Task 07 — AWS deployment (EC2 + S3 + CloudFront)

**Model:** lower model OK for writing files. **A human runs the AWS commands** (billing, IAM). **Time:** ~1 day. **Depends on:** Tasks 04, 04b and 05.

## Target layout
```
Users ─► CloudFront (https://<dist>.cloudfront.net or custom domain)
          ├─ /stacks/*, /catalog/*, /catalog_public.json  → S3 bucket  spherex-blink-stacks  (OAC, private bucket)
          ├─ /api/*                                        → EC2 origin (Caddy :443 → uvicorn :8000)
          └─ /* (default)                                  → EC2 origin (StaticFiles serves the frontend)
EC2 serving:  t4g.medium (ARM), Ubuntu 24.04, gp3 50 GB, region = SPHEREx bucket region (recon R7)
EC2 batch:    c7g.2xlarge spot, launched weekly by EventBridge → runs sync + preprocess → shuts down
```
Same origin for everything, so there are no CORS issues inside the app. The API keeps `Access-Control-Allow-Origin: *` for external embedders.

## Files to create
- `deploy/Dockerfile`
- `deploy/docker-compose.yml`
- `deploy/Caddyfile`
- `deploy/iam-serving-policy.json`, `deploy/iam-batch-policy.json`
- `deploy/batch-userdata.sh`
- `deploy/RUNBOOK.md`: every command below, in order, with expected output

## Steps

### D1. Dockerfile (multi-arch)
```dockerfile
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system --no-cache --only-binary :all: -r requirements.txt
COPY backend backend
COPY frontend frontend
COPY scripts scripts
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
```
Mount `/app/data` from the host EBS volume. `requirements.txt` holds runtime deps only (Task 00 split out the dev deps). Pin the uv image to a specific version tag instead of `latest` when you write the file.

### D2. docker-compose.yml
Two services: `api` (above, volume `/data:/app/data`, `restart: unless-stopped`, env from `.env`) and `caddy` (`caddy:2`, ports 80/443, mount the Caddyfile and a caddy_data volume).

### D3. Caddyfile
```
{$DOMAIN} {
    encode zstd gzip
    reverse_proxy api:8000 {
        flush_interval -1          # REQUIRED: don't buffer NDJSON streams
    }
    header {
        Content-Security-Policy "frame-ancestors *"
        -X-Frame-Options
    }
}
```
If there's no custom domain, use CloudFront → EC2 over HTTP on port 80, with the security group allowing only the CloudFront managed prefix list.

### D4. S3 stacks bucket
```bash
aws s3api create-bucket --bucket spherex-blink-stacks --region <region> [--create-bucket-configuration LocationConstraint=<region> if not us-east-1]
aws s3api put-public-access-block --bucket spherex-blink-stacks \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```
Lifecycle rule: expire `stacks/` objects whose `data_version` prefix is older than 30 days. Prefixes are versioned, so a simple age rule on `stacks/` works.

### D5. IAM
- Serving role: `s3:GetObject`/`s3:ListBucket` on the SPHEREx public bucket (if FETCH_METHOD=s3), and `s3:GetObject`/`HeadObject` on `spherex-blink-stacks/*`.
- Batch role: the same, plus `s3:PutObject` on `spherex-blink-stacks/*`, plus `ec2:StopInstances` on itself (self-shutdown).
- Attach via instance profiles. **No access keys on disk.**

### D6. Serving EC2
1. Launch a t4g.medium with Ubuntu 24.04 ARM, the instance profile, and a security group: 22 from your IP only; 80/443 from the CloudFront prefix list (or 0.0.0.0/0 if Caddy terminates TLS for a custom domain).
2. Install Docker + compose. Clone the repo. Create `/data`.
3. **Copy the index:** `aws s3 cp s3://spherex-blink-stacks/index/spherex.sqlite /data/index/` (the batch instance builds and uploads it in D8), or build it once on this box (`python scripts/build_index.py`, overnight).
4. `docker compose up -d`. Check `curl localhost:8000/api/health`.

### D7. CloudFront
- Origins: S3 (with OAC; add the bucket policy for OAC) and EC2 (public DNS or domain).
- Behaviours, in order:
  1. `/stacks/*` → S3. Cache policy CachingOptimized. **Do not compress** (the objects are already gzip with Content-Encoding set).
  2. `/catalog/*`, `/catalog_public.json` → S3. CachingOptimized.
  3. `/api/sequence*` → EC2. **CachingDisabled**, origin request policy AllViewerExceptHostHeader, response streaming works by default.
  4. `/api/resolve*` → EC2. Cache by query string (cache policy with all query strings), TTL 1 day.
  5. Default `/*` → EC2. CachingOptimized with a short TTL (5 min) for the frontend.
- Set `config.STACKS_PUBLIC_BASE = "/stacks/"` (same origin), so the API's 307 redirect stays on the same domain.

### D8. Weekly batch
`deploy/batch-userdata.sh` (runs on boot of the spot instance):
```bash
#!/bin/bash
set -euo pipefail
cd /opt/app && git pull && source .venv/bin/activate
aws s3 cp s3://spherex-blink-stacks/index/spherex.sqlite data/index/spherex.sqlite || true
python scripts/sync_index.py
aws s3 cp data/index/spherex.sqlite s3://spherex-blink-stacks/index/spherex.sqlite
python scripts/preprocess_catalog.py --catalog data/catalog.csv --changed-only --upload
aws s3 cp data/batch_report.json s3://spherex-blink-stacks/reports/$(date +%F).json
shutdown -h now
```
- EventBridge Scheduler, weekly (e.g. Monday 06:00 UTC) → `ec2:StartInstances` on a stopped batch instance. A stopped instance + userdata via cloud-init `per-boot` is simpler than launching a fresh spot each time. Document whichever you choose.
- The serving box picks up the new index with a daily cron: `aws s3 cp ... && docker compose restart api` **only if the ETag changed**.

### D9. Guard rails
- API: per-IP rate limit, 30 `/api/sequence` requests per minute. Use `slowapi`, or a 20-line in-memory token bucket in `backend/ratelimit.py` with a test.
- `MAX_FOV_DEG` is enforced (already done in Task 04).
- L1 cache cap: a cron runs `scripts/prune_cache.py --max-gb 30`, deleting the oldest-atime files first.
- AWS Budgets: a monthly alarm at $50 (adjust to the user's budget) emailing the owner.
- CloudWatch agent: ship the docker logs. Alarms on 5xx > 5% for 5 min and on the instance status check.

### D10. Embeddability check
On a different origin (e.g. a local `python -m http.server` page), embed:
```html
<iframe src="https://<dist>/?ra=133.795&dec=-7.245&size=0.1&det=6&mode=pass&embed=1" width="420" height="480"></iframe>
```
It must load and play.

### D11. Production benchmark
`python scripts/bench.py --base https://<dist>` (cached), and `--cold` on 3 non-precomputed random positions. All plan.md §3 budgets must PASS. Paste the table into `notes/bench_prod.md`.

## Done when
- D10 works (screenshot).
- D11: all budgets PASS, or the failures are listed with a cause and a fix plan.
- `deploy/RUNBOOK.md` lets someone else redeploy from scratch.
- The budget alarm exists.

## Do NOT
- Put AWS keys in the repo, `.env` or the image.
- Enable CloudFront compression on `/stacks/*`.
- Expose port 8000 publicly.
