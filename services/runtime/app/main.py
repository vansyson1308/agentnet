"""Codex managed runtime using isolated per-run Git worktrees."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:8000").rstrip("/")
RUNTIME_ID = os.environ["RUNTIME_ID"]
RUNTIME_TOKEN = os.environ["RUNTIME_TOKEN"]
WORK_ROOT = Path(os.environ.get("RUNTIME_WORK_ROOT", "/worktrees")).resolve()
REPOSITORY_ALLOWLIST = [
    item.strip() for item in os.environ.get("RUNTIME_REPOSITORY_ALLOWLIST", "").split(",") if item.strip()
]
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
# Remove the API key from the long-lived runtime environment. It is supplied
# only to the non-interactive Codex child process, never to Git commands.
CODEX_API_KEY = os.environ.pop("CODEX_API_KEY")


def _run(
    args: list[str],
    cwd: Path,
    timeout: int,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, timeout=timeout, check=check, capture_output=True, text=False, env=env)


def _allowed(repository: str) -> bool:
    return any(pattern == "*" or fnmatch.fnmatchcase(repository, pattern) for pattern in REPOSITORY_ALLOWLIST)


async def _post(client: httpx.AsyncClient, path: str, payload: dict) -> httpx.Response:
    response = await client.post(
        f"{REGISTRY_URL}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {RUNTIME_TOKEN}"},
    )
    response.raise_for_status()
    return response


async def _heartbeat(client: httpx.AsyncClient, assignment: dict, stop: asyncio.Event) -> None:
    sequence = 1
    while not stop.is_set():
        try:
            await _post(
                client,
                f"/v1/runs/{assignment['run_id']}/heartbeat",
                {"lease_token": assignment["lease_token"], "sequence": sequence},
            )
            sequence += 1
        except Exception:
            # The terminal command remains authoritative; a transient
            # heartbeat failure is retried until the lease expires.
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            continue


async def _upload(client: httpx.AsyncClient, assignment: dict, artifact_type: str, content: bytes, mime: str, **extra):
    digest = hashlib.sha256(content).hexdigest()
    payload = {
        "lease_token": assignment["lease_token"],
        "artifact_type": artifact_type,
        "sha256": digest,
        "size_bytes": len(content),
        "mime_type": mime,
        "content_base64": base64.b64encode(content).decode("ascii"),
        **extra,
    }
    await _post(client, f"/v1/runs/{assignment['run_id']}/artifacts", payload)


async def execute_assignment(client: httpx.AsyncClient, assignment: dict) -> None:
    if not _allowed(assignment["repository"]):
        raise RuntimeError("repository is outside this runtime's local allowlist")
    run_dir = (WORK_ROOT / assignment["run_id"]).resolve()
    if WORK_ROOT not in run_dir.parents:
        raise RuntimeError("invalid worktree path")
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.parent.mkdir(parents=True, exist_ok=True)

    sequence = 1
    await _post(
        client,
        f"/v1/runs/{assignment['run_id']}/events",
        {
            "lease_token": assignment["lease_token"],
            "sequence": sequence,
            "idempotency_key": f"runtime:{assignment['run_id']}:started",
            "trace_id": assignment["trace_id"],
            "event_type": "run.started",
            "payload": {},
        },
    )
    timeout = min(max(int(assignment.get("budgets", {}).get("time_seconds", 3600)), 30), 86400)
    transcript = bytearray()
    try:
        clone = _run(
            ["git", "clone", "--no-checkout", "--", assignment["repository"], str(run_dir)], WORK_ROOT, timeout
        )
        transcript.extend(clone.stdout + clone.stderr)
        checkout = _run(["git", "checkout", "--detach", assignment["base_commit_sha"]], run_dir, timeout)
        transcript.extend(checkout.stdout + checkout.stderr)
        codex_env = {**os.environ, "CODEX_API_KEY": CODEX_API_KEY}
        codex = _run(
            [CODEX_BIN, "exec", "--full-auto", assignment["prompt"]],
            run_dir,
            timeout,
            check=False,
            env=codex_env,
        )
        transcript.extend(codex.stdout + codex.stderr)
        if codex.returncode != 0:
            raise RuntimeError(f"Codex exited with status {codex.returncode}")

        status_result = _run(["git", "status", "--porcelain"], run_dir, 30)
        if status_result.stdout.strip():
            _run(["git", "add", "-A"], run_dir, 30)
            _run(
                [
                    "git",
                    "-c",
                    "user.name=AgentNet Codex Runtime",
                    "-c",
                    "user.email=runtime@agentnet.invalid",
                    "commit",
                    "-m",
                    f"AgentNet managed run {assignment['run_id']}",
                ],
                run_dir,
                60,
            )
        candidate = _run(["git", "rev-parse", "HEAD"], run_dir, 30).stdout.decode().strip()
        patch = _run(["git", "diff", "--binary", f"{assignment['base_commit_sha']}..{candidate}"], run_dir, 60).stdout
        changed = (
            _run(["git", "diff", "--name-only", f"{assignment['base_commit_sha']}..{candidate}"], run_dir, 30)
            .stdout.decode()
            .splitlines()
        )
        manifest = json.dumps(
            {
                "base_commit_sha": assignment["base_commit_sha"],
                "candidate_commit_sha": candidate,
                "changed_files": changed,
            },
            sort_keys=True,
        ).encode()
        await _upload(
            client,
            assignment,
            "manifest",
            manifest,
            "application/json",
            base_commit_sha=assignment["base_commit_sha"],
            candidate_commit_sha=candidate,
            changed_files=changed,
        )
        await _upload(
            client,
            assignment,
            "patch",
            patch,
            "text/x-diff",
            base_commit_sha=assignment["base_commit_sha"],
            candidate_commit_sha=candidate,
            changed_files=changed,
        )
        if transcript:
            await _upload(client, assignment, "log", bytes(transcript[-10 * 1024 * 1024 :]), "text/plain")
        await _post(
            client,
            f"/v1/runs/{assignment['run_id']}/events",
            {
                "lease_token": assignment["lease_token"],
                "sequence": sequence + 1,
                "idempotency_key": f"runtime:{assignment['run_id']}:artifacts",
                "trace_id": assignment["trace_id"],
                "event_type": "run.artifact_submitted",
                "payload": {"candidate_commit_sha": candidate},
            },
        )
        sequence += 1
        await _post(
            client,
            f"/v1/runs/{assignment['run_id']}/complete",
            {
                "lease_token": assignment["lease_token"],
                "sequence": sequence + 1,
                "idempotency_key": f"runtime:{assignment['run_id']}:complete",
                "trace_id": assignment["trace_id"],
                "candidate_commit_sha": candidate,
            },
        )
        sequence += 1
    except Exception as exc:
        await _post(
            client,
            f"/v1/runs/{assignment['run_id']}/fail",
            {
                "lease_token": assignment["lease_token"],
                "sequence": sequence + 1,
                "idempotency_key": f"runtime:{assignment['run_id']}:failed",
                "trace_id": assignment["trace_id"],
                "error": str(exc)[:2000],
            },
        )
        raise


async def main() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=40.0) as client:
        while True:
            response = await _post(client, f"/v1/runtimes/{RUNTIME_ID}/assignments/claim?wait_seconds=30", {})
            if response.status_code == 204 or not response.content:
                continue
            assignment = response.json()
            stop = asyncio.Event()
            heartbeat = asyncio.create_task(_heartbeat(client, assignment, stop))
            try:
                await execute_assignment(client, assignment)
            finally:
                stop.set()
                await heartbeat


if __name__ == "__main__":
    asyncio.run(main())
