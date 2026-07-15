"""HyperFrames on Modal — preview + render template.

Deploy:  modal deploy src/app.py
Dev:     modal serve src/app.py
Smoke:   modal run src/app.py            (renders compositions/smoke end-to-end)

Architecture (mirrors hyperframes-vercel-template / hyperframes-cloudflare-template):
- A web endpoint serves a preview page (<hyperframes-player>) for the bundled composition.
- POST /api/render spawns a render Function (headless Chromium + FFmpeg via the
  hyperframes CLI) and returns a call_id; the client polls GET /api/render/{call_id}.
  (Modal web requests cap at 150s, so spawn+poll is the blessed pattern.)
- Finished MP4s land on a Modal Volume and are served back via GET /renders/{name}.
"""

import hashlib
import json
import os
import pathlib
import time
import uuid
import urllib.parse

import modal

HYPERFRAMES_VERSION = "0.7.41"
HYPERFRAMES_NPM_PREFIX = "/opt/hyperframes-cli"
HYPERFRAMES_BIN = f"{HYPERFRAMES_NPM_PREFIX}/node_modules/.bin/hyperframes"
MINUTES = 60  # seconds

# The composition bundled into the preview page + default render target.
# Swap this to change what the template previews/renders.
PREVIEW_COMPOSITION = "modal-intro"

root = pathlib.Path(__file__).resolve().parent.parent

app = modal.App("hyperframes-modal")
production_secret = modal.Secret.from_name("Production")

# System libraries chrome-headless-shell needs on Debian bookworm.
CHROMIUM_DEPS = [
    "libnss3",
    "libnspr4",
    "libatk1.0-0",
    "libatk-bridge2.0-0",
    "libcups2",
    "libdrm2",
    "libxkbcommon0",
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libgbm1",
    "libasound2",
    "libpango-1.0-0",
    "libcairo2",
    "libx11-6",
    "libxcb1",
    "libxext6",
    "fonts-liberation",
]

image = (
    modal.Image.from_registry("node:22-bookworm-slim", add_python="3.12")
    .apt_install("ffmpeg", "ca-certificates", *CHROMIUM_DEPS)
    .run_commands(
        f"npm install --prefix {HYPERFRAMES_NPM_PREFIX} hyperframes@{HYPERFRAMES_VERSION}",
        f"{HYPERFRAMES_BIN} browser ensure",
        f"{HYPERFRAMES_BIN} browser path",
    )
    .env(
        {
            "PATH": f"{HYPERFRAMES_NPM_PREFIX}/node_modules/.bin:/usr/local/bin:/usr/bin:/bin"
        }
    )
    .uv_pip_install("fastapi[standard]==0.139.0", "itsdangerous==2.2.0", "boto3==1.41.4")
    .add_local_dir(root / "compositions", remote_path="/compositions")
    .add_local_dir(root / "templates", remote_path="/templates")
    .add_local_dir(root / "web", remote_path="/assets")
)

renders = modal.Volume.from_name("hyperframes-renders", create_if_missing=True)
studio_assets = modal.Volume.from_name("hyperframes-studio-asset-cache", create_if_missing=True)
projects = modal.Dict.from_name("hyperframes-studio-projects", create_if_missing=True)
assets = modal.Dict.from_name("hyperframes-studio-assets", create_if_missing=True)
render_jobs = modal.Dict.from_name("hyperframes-studio-render-jobs", create_if_missing=True)

RENDERS_DIR = "/renders"
ASSETS_DIR = "/assets-store"


def now() -> int:
    return int(time.time())


def load_template(template_id: str) -> dict:
    path = pathlib.Path("/templates") / f"{template_id}.json"
    if not path.is_file():
        raise ValueError(f"unknown template: {template_id}")
    return json.loads(path.read_text())


def manifest_for(project: dict, template: dict) -> dict:
    return {
        "schemaVersion": 1,
        "projectId": project["id"],
        "templateId": template["id"],
        "templateVersion": template["version"],
        "copy": project.get("copy", {}),
        "assets": project.get("assets", []),
        "output": template["defaultOutput"],
        "createdAt": now(),
    }


def r2_enabled() -> bool:
    keys = ["R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    return all(os.environ.get(key) for key in keys)


def r2_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("R2_REGION", "auto"),
    )


def validate_project(project: dict, template: dict) -> None:
    copy = project.get("copy", {})
    missing = [
        field["label"]
        for field in template["fields"]
        if field.get("required") and not copy.get(field["key"])
    ]
    asset_keys = {asset["key"] for asset in project.get("assets", [])}
    missing += [
        asset["label"]
        for asset in template["requiredAssets"]
        if asset["key"] not in asset_keys
    ]
    if missing:
        raise ValueError("Missing required inputs: " + ", ".join(missing))


@app.function(
    image=image,
    timeout=20 * MINUTES,
    volumes={RENDERS_DIR: renders, ASSETS_DIR: studio_assets},
    max_containers=1,
)
def render_studio_job(job_id: str) -> None:
    import shutil
    import subprocess

    job = render_jobs[job_id]
    job["status"] = "rendering"
    job["updatedAt"] = now()
    render_jobs[job_id] = job

    manifest = job["manifest"]
    work = pathlib.Path(f"/tmp/render-{job_id}")
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(pathlib.Path("/compositions") / manifest["templateId"], work / "composition")
    (work / "composition" / "manifest.json").write_text(json.dumps(manifest))

    out = work / "out.mp4"
    try:
        subprocess.run(
            [
                "hyperframes",
                "render",
                "composition",
                "-o",
                str(out),
                "--workers",
                "auto",
                "--no-browser-gpu",
            ],
            cwd=work,
            check=True,
        )
        name = f"{job_id}.mp4"
        shutil.copy(out, f"{RENDERS_DIR}/{name}")
        renders.commit()
        job.update(
            {
                "status": "complete",
                "output": {
                    "name": name,
                    "downloadUrl": f"/api/projects/{job['projectId']}/download?renderId={job_id}",
                },
                "updatedAt": now(),
            }
        )
    except Exception as exc:
        job.update({"status": "failed", "error": str(exc), "updatedAt": now()})
    render_jobs[job_id] = job


@app.function(
    image=image,
    timeout=15 * MINUTES,
    volumes={RENDERS_DIR: renders},
)
def render_composition(composition: str = PREVIEW_COMPOSITION) -> str:
    """Render one bundled composition to MP4 and store it on the Volume.

    Returns the filename on the Volume (serve via GET /renders/{name}).
    """
    import shutil
    import subprocess
    import uuid

    src = pathlib.Path("/compositions") / composition
    if not src.is_dir():
        raise ValueError(f"unknown composition: {composition!r}")

    work = pathlib.Path("/tmp/render-job")
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(src, work / "composition")

    out = work / "out.mp4"
    subprocess.run(
        [
            "hyperframes",
            "render",
            "composition",
            "-o",
            str(out),
            "--workers",
            "auto",
            # No GPU in Modal containers: skip the GPU probe, which otherwise
            # hangs to puppeteer's 180s protocolTimeout before falling back.
            "--no-browser-gpu",
        ],
        cwd=work,
        check=True,
    )

    name = f"{composition}-{uuid.uuid4().hex[:8]}.mp4"
    shutil.copy(out, f"{RENDERS_DIR}/{name}")
    renders.commit()
    return name


@app.function(
    image=image,
    volumes={RENDERS_DIR: renders, ASSETS_DIR: studio_assets},
    secrets=[production_secret],
)
@modal.asgi_app()
def web():
    import hmac

    import fastapi
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles

    from itsdangerous import BadSignature, URLSafeSerializer

    api = fastapi.FastAPI(title="HyperFrames on Modal")
    signer = URLSafeSerializer(
        os.environ["SESSION_SECRET"],
        salt="studio-session",
    )

    def current_user(request: fastapi.Request) -> str:
        token = request.cookies.get("studio_session")
        try:
            data = signer.loads(token or "")
        except BadSignature:
            raise fastapi.HTTPException(status_code=401, detail="Sign in required")
        if data.get("sub") != "owner":
            raise fastapi.HTTPException(status_code=401, detail="Sign in required")
        return "owner"

    @api.post("/api/login")
    async def login(body: dict, response: fastapi.Response):
        expected = os.environ["STUDIO_PASSWORD"]
        if not hmac.compare_digest(str(body.get("password", "")), expected):
            raise fastapi.HTTPException(status_code=401, detail="Invalid password")
        response.set_cookie(
            "studio_session",
            signer.dumps({"sub": "owner", "iat": now()}),
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=86400,
        )
        return {"ok": True}

    @api.post("/api/logout")
    async def logout(response: fastapi.Response):
        response.delete_cookie("studio_session")
        return {"ok": True}

    @api.get("/api/templates")
    async def list_templates(_: str = fastapi.Depends(current_user)):
        return {
            "templates": [
                json.loads(path.read_text())
                for path in sorted(pathlib.Path("/templates").glob("*.json"))
            ]
        }

    @api.post("/api/projects")
    async def create_project(body: dict, _: str = fastapi.Depends(current_user)):
        template = load_template(body["templateId"])
        project_id = uuid.uuid4().hex
        project = {
            "id": project_id,
            "templateId": template["id"],
            "state": "draft",
            "copy": {},
            "assets": [],
            "createdAt": now(),
            "updatedAt": now(),
        }
        projects[project_id] = project
        return project

    @api.post("/api/assets/upload-url")
    async def upload_url(body: dict, _: str = fastapi.Depends(current_user)):
        asset_id = uuid.uuid4().hex
        filename = pathlib.Path(body["filename"]).name
        object_key = f"projects/{body['projectId']}/{asset_id}/{filename}"
        asset = {
            "id": asset_id,
            "projectId": body["projectId"],
            "key": body["key"],
            "filename": filename,
            "contentType": body.get("contentType"),
            "objectKey": object_key,
            "url": f"/api/assets/{asset_id}/download",
            "previewUrl": f"/api/assets/{asset_id}/download",
        }
        assets[asset_id] = asset
        if r2_enabled():
            upload = r2_client().generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": os.environ["R2_BUCKET"],
                    "Key": object_key,
                    "ContentType": body.get("contentType") or "application/octet-stream",
                },
                ExpiresIn=900,
            )
            return {"asset": asset, "uploadUrl": upload}
        return {"asset": asset, "uploadUrl": f"/api/assets/{asset_id}/upload"}

    @api.put("/api/assets/{asset_id}/upload")
    async def receive_upload(
        asset_id: str,
        request: fastapi.Request,
        _: str = fastapi.Depends(current_user),
    ):
        if asset_id not in assets:
            raise fastapi.HTTPException(status_code=404)
        path = pathlib.Path(ASSETS_DIR) / asset_id
        path.write_bytes(await request.body())
        studio_assets.commit()
        return {"ok": True}

    @api.get("/api/assets/{asset_id}/download")
    async def asset_download(asset_id: str, _: str = fastapi.Depends(current_user)):
        asset = assets[asset_id]
        if r2_enabled() and asset.get("objectKey"):
            url = r2_client().generate_presigned_url(
                "get_object",
                Params={"Bucket": os.environ["R2_BUCKET"], "Key": asset["objectKey"]},
                ExpiresIn=900,
            )
            return RedirectResponse(url)
        studio_assets.reload()
        path = pathlib.Path(ASSETS_DIR) / asset_id
        if not path.is_file():
            raise fastapi.HTTPException(status_code=404)
        return FileResponse(
            path,
            media_type=asset.get("contentType") or "application/octet-stream",
        )

    @api.patch("/api/projects/{project_id}")
    async def update_project(
        project_id: str,
        body: dict,
        _: str = fastapi.Depends(current_user),
    ):
        project = projects[project_id]
        project["copy"] = body.get("copy", project.get("copy", {}))
        project["assets"] = body.get("assets", project.get("assets", []))
        project["state"] = "ready"
        project["updatedAt"] = now()
        projects[project_id] = project
        return project

    @api.get("/api/projects/{project_id}/preview")
    async def preview(project_id: str, _: str = fastapi.Depends(current_user)):
        project = projects[project_id]
        template = load_template(project["templateId"])
        html = pathlib.Path(f"/compositions/{template['id']}/index.html").read_text()
        runtime = (
            '<script src="https://cdn.jsdelivr.net/npm/@hyperframes/core@'
            f'{HYPERFRAMES_VERSION}/dist/hyperframe.runtime.iife.js"></script>'
        )
        html = html.replace("</head>", f"{runtime}</head>", 1)
        manifest = urllib.parse.quote(json.dumps(manifest_for(project, template)))
        return HTMLResponse(
            html.replace("./manifest.json", f"data:application/json,{manifest}")
        )

    @api.post("/api/projects/{project_id}/render")
    async def start_studio_render(
        project_id: str,
        _: str = fastapi.Depends(current_user),
    ):
        project = projects[project_id]
        template = load_template(project["templateId"])
        validate_project(project, template)
        manifest = manifest_for(project, template)
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()[:16]
        existing = [
            job
            for job in render_jobs.values()
            if job.get("projectId") == project_id
            and job.get("digest") == digest
            and job.get("status") in {"queued", "rendering", "complete"}
        ]
        if existing:
            return existing[0]
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "projectId": project_id,
            "templateId": template["id"],
            "digest": digest,
            "manifest": manifest,
            "status": "queued",
            "retryCount": 0,
            "createdAt": now(),
            "updatedAt": now(),
        }
        render_jobs[job_id] = job
        render_studio_job.spawn(job_id)
        return job

    @api.get("/api/renders/{render_id}")
    async def get_studio_render(
        render_id: str,
        _: str = fastapi.Depends(current_user),
    ):
        job = render_jobs[render_id]
        if job.get("output"):
            job["downloadUrl"] = job["output"]["downloadUrl"]
        return job

    @api.post("/api/renders/{render_id}/retry")
    async def retry_studio_render(
        render_id: str,
        _: str = fastapi.Depends(current_user),
    ):
        job = render_jobs[render_id]
        if job["status"] != "failed" or job.get("retryCount", 0) >= 1:
            raise fastapi.HTTPException(
                status_code=400,
                detail="Retry is only available once after failure",
            )
        new_id = uuid.uuid4().hex
        new_job = {
            **job,
            "id": new_id,
            "status": "queued",
            "retryCount": job.get("retryCount", 0) + 1,
            "createdAt": now(),
            "updatedAt": now(),
        }
        render_jobs[new_id] = new_job
        render_studio_job.spawn(new_id)
        return new_job

    @api.get("/api/projects/{project_id}/download")
    async def download_project_render(
        project_id: str,
        renderId: str,
        _: str = fastapi.Depends(current_user),
    ):
        job = render_jobs[renderId]
        if job["projectId"] != project_id or job["status"] != "complete":
            raise fastapi.HTTPException(status_code=404)
        renders.reload()
        name = job["output"]["name"]
        return FileResponse(
            pathlib.Path(RENDERS_DIR) / name,
            media_type="video/mp4",
            filename=name,
        )

    @api.post("/api/render")
    def start_render() -> dict:
        call = render_composition.spawn(PREVIEW_COMPOSITION)
        return {"call_id": call.object_id}

    @api.get("/api/render/{call_id}")
    def poll_render(call_id: str):
        fc = modal.FunctionCall.from_id(call_id)
        try:
            name = fc.get(timeout=0)
        except TimeoutError:
            return JSONResponse({"status": "running"}, status_code=202)
        except Exception as exc:  # remote render failure surfaces here
            return JSONResponse(
                {"status": "failed", "error": str(exc)}, status_code=500
            )
        return {"status": "done", "url": f"/renders/{name}"}

    @api.get("/renders/{name}")
    def get_render(name: str):
        if "/" in name or ".." in name:
            raise fastapi.HTTPException(status_code=400)
        renders.reload()
        path = pathlib.Path(RENDERS_DIR) / name
        if not path.is_file():
            raise fastapi.HTTPException(status_code=404)
        return FileResponse(path, media_type="video/mp4")

    @api.get("/composition/")
    def composition_html():
        """Serve the preview composition with the seek runtime injected.

        The <hyperframes-player> drives the composition frame-by-frame via the
        hyperframe runtime; compositions don't bake it in, so inject it here
        (same trick as the Vercel template's normalizePreviewHtml).
        """
        html = pathlib.Path(
            f"/compositions/{PREVIEW_COMPOSITION}/index.html"
        ).read_text()
        runtime = (
            '<script src="https://cdn.jsdelivr.net/npm/@hyperframes/core@'
            f'{HYPERFRAMES_VERSION}/dist/hyperframe.runtime.iife.js"></script>'
        )
        html = html.replace("</head>", f"{runtime}\n</head>", 1)
        return HTMLResponse(html)

    # Route order matters: the explicit /composition/ route above wins over this
    # mount, which serves the composition's relative assets (assets/*.png, ...).
    api.mount(
        "/composition",
        StaticFiles(directory=f"/compositions/{PREVIEW_COMPOSITION}"),
        name="composition-assets",
    )
    api.mount("/", StaticFiles(directory="/assets", html=True), name="frontend")

    return api


@app.local_entrypoint()
def smoke(composition: str = "smoke"):
    """End-to-end smoke test: render the tiny bundled composition remotely."""
    print(f"rendering {composition!r} on Modal ...")
    name = render_composition.remote(composition)
    print(f"done: {name}")
    print(f"download: modal volume get hyperframes-renders {name} {name}")
