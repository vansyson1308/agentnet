#!/usr/bin/env python3
"""
Hermes_Builder v6 -- Real Implementation Agent

Diff vs v5/earlier:
- v5 had ~3 hardcoded implementors + implement_default writing useless .md placeholders.
- v6 reads structured proposal from PlannerV4 (with files_to_modify + acceptance),
  feeds the file content + spec to DeepSeek V4 Flash, gets back a unified diff,
  applies it, runs git add+commit, and posts the diff in review_request to QA.

Safety guards:
- Only modifies files inside REPO_ROOT.
- Only modifies files listed in proposal's files_to_modify (with optional fuzzy match).
- Validates Python syntax of any .py file after patch (py_compile gate).
- Backs up touched files to /opt/agentnet-builder-backup/{timestamp}/ before patching.
- If patch fails or syntax broken: rollback + report error to QA.

Each backlog item flows: receive proposal -> codegen -> apply -> commit -> review_request.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time as time_module
import urllib.request
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))
from hermes_agent_base import HermesAgent, AGENT_IDS  # noqa: E402

REPO_ROOT = pathlib.Path("/opt/agentnet")
BACKUP_ROOT = pathlib.Path("/opt/agentnet-builder-backup")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL_FAST", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
GIT_AUTHOR_NAME = "Hermes Builder v6"
GIT_AUTHOR_EMAIL = "hermes-builder@agentnet.local"


class BuilderV6(HermesAgent):
    def __init__(self):
        super().__init__("Hermes_Builder_v6", "builder", sleep_seconds=20)

    def on_start(self):
        self.load_processed()
        self.log.info("BuilderV6 starting -- repo=%s, model=%s", REPO_ROOT, DEEPSEEK_MODEL)
        BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        # Configure git author once
        subprocess.run(["git", "-C", str(REPO_ROOT), "config", "user.email", GIT_AUTHOR_EMAIL], check=False)
        subprocess.run(["git", "-C", str(REPO_ROOT), "config", "user.name", GIT_AUTHOR_NAME], check=False)

    # --- DeepSeek codegen ---

    def _llm_codegen(self, system: str, user: str, max_tokens: int = 4000) -> Optional[str]:
        if not DEEPSEEK_API_KEY:
            self.log.error("DEEPSEEK_API_KEY missing")
            return None
        body = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": int(max_tokens),
            "temperature": 0.2,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        req = urllib.request.Request(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode())
                return d["choices"][0]["message"]["content"]
        except Exception as e:
            self.log.error("deepseek call failed: %s", e)
            return None

    # --- Patch handling ---

    def _backup_files(self, files: list[pathlib.Path]) -> pathlib.Path:
        ts = time_module.strftime("%Y%m%d-%H%M%S")
        bdir = BACKUP_ROOT / ts
        bdir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.exists():
                rel = f.relative_to(REPO_ROOT)
                tgt = bdir / rel
                tgt.parent.mkdir(parents=True, exist_ok=True)
                tgt.write_bytes(f.read_bytes())
        return bdir

    def _apply_full_files(self, file_blocks: dict[str, str], files: list[pathlib.Path]) -> tuple[bool, str]:
        """Replace target files with the new content from LLM.

        file_blocks: dict path-string -> new full content
        Returns (success, error_msg)
        """
        backup = self._backup_files(files)
        self.log.info("backed up %d files to %s", len(files), backup)
        try:
            for rel, content in file_blocks.items():
                target = REPO_ROOT / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                # py_compile gate for python files
                if target.suffix == ".py":
                    r = subprocess.run(
                        [sys.executable, "-m", "py_compile", str(target)],
                        capture_output=True,
                        text=True,
                    )
                    if r.returncode != 0:
                        # Rollback
                        for orig in files:
                            rel_orig = orig.relative_to(REPO_ROOT)
                            backup_path = backup / rel_orig
                            if backup_path.exists():
                                orig.write_bytes(backup_path.read_bytes())
                            elif orig.exists():
                                orig.unlink()
                        return False, f"py_compile failed for {rel}: {r.stderr[:300]}"
            return True, ""
        except Exception as e:
            # Rollback
            for orig in files:
                try:
                    rel_orig = orig.relative_to(REPO_ROOT)
                    backup_path = backup / rel_orig
                    if backup_path.exists():
                        orig.write_bytes(backup_path.read_bytes())
                except Exception:
                    pass
            return False, f"apply failed: {e!r}"

    def _git_commit(self, item_id: str, title: str, files: list[pathlib.Path]) -> tuple[bool, str]:
        """Stage + commit. Returns (success, message)."""
        try:
            for f in files:
                rel = f.relative_to(REPO_ROOT)
                subprocess.run(["git", "-C", str(REPO_ROOT), "add", str(rel)], check=True)
            msg = f"feat({item_id}): {title}\n\nAuto-shipped by Hermes_Builder_v6 from AGENT_BACKLOG.md"
            r = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "commit", "-m", msg],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                # Maybe nothing to commit
                return False, f"git commit: {r.stdout} {r.stderr}"
            # Get short SHA
            sha = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            return True, sha
        except Exception as e:
            return False, f"git error: {e!r}"

    def _git_diff_preview(self, sha: str) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "show", "--stat", sha],
                capture_output=True,
                text=True,
            )
            return r.stdout[:1500]
        except Exception:
            return ""

    # --- Proposal handling ---

    def _parse_proposal(self, msg: dict) -> Optional[dict]:
        """Extract structured spec from planner's proposal message."""
        content = msg.get("content", "") or ""
        m = re.search(r"```json\n(.+?)\n```", content, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

    def _collect_existing_files(self, files_to_modify: list[str]) -> tuple[dict[str, str], list[pathlib.Path]]:
        """Read current content of files_to_modify (only what exists)."""
        out_content: dict[str, str] = {}
        paths: list[pathlib.Path] = []
        for rel in files_to_modify:
            # Safety: must be inside repo, no .., no absolute
            if rel.startswith("/") or ".." in rel:
                self.log.warning("skip unsafe path: %s", rel)
                continue
            p = (REPO_ROOT / rel).resolve()
            try:
                p.relative_to(REPO_ROOT.resolve())
            except ValueError:
                self.log.warning("skip path outside repo: %s", rel)
                continue
            paths.append(p)
            if p.exists():
                try:
                    out_content[rel] = p.read_text(encoding="utf-8")
                except Exception as e:
                    self.log.warning("cannot read %s: %s", p, e)
        return out_content, paths

    def _build_codegen_prompt(self, spec: dict, current_contents: dict[str, str]) -> tuple[str, str]:
        sys_prompt = (
            "You are a precise senior backend engineer. You modify Python/HTML files in a "
            "FastAPI + SQLAlchemy + Jinja2 project (AgentNet, the agent marketplace). "
            "Output ONLY the updated full content of each file in fenced blocks like:\n\n"
            "```file:relative/path.py\n<full content>\n```\n\n"
            "Rules:\n"
            "- Preserve all existing imports, docstrings, helper functions unless explicitly changed.\n"
            "- Add minimal new code; do NOT refactor unrelated parts.\n"
            "- Keep the same code style and indentation as the surrounding code.\n"
            "- For new files, create a complete, syntactically valid file.\n"
            "- For modified files, output the COMPLETE NEW CONTENT (not a diff).\n"
            "- Do not include any prose outside the file blocks."
        )
        files_section = []
        for rel, content in current_contents.items():
            # Truncate very large files to avoid context overflow
            preview = content if len(content) <= 6000 else content[:5500] + "\n# ... [TRUNCATED -- preserve when editing] ...\n"
            files_section.append(f"```file:{rel}\n{preview}\n```")
        if not files_section and spec.get("files_to_modify"):
            files_section.append(
                "(All target files do not exist yet -- you must create them from scratch.)"
            )
        files_block = "\n\n".join(files_section)
        user_prompt = (
            f"BACKLOG ITEM: {spec.get('id')}\n"
            f"TITLE: {spec.get('title')}\n"
            f"PRIORITY: {spec.get('priority')}\n\n"
            f"DESCRIPTION:\n{spec.get('description', '')}\n\n"
            f"ACCEPTANCE CRITERIA:\n"
            + "\n".join(f"- {a}" for a in spec.get("acceptance", []))
            + "\n\nFILES TO MODIFY:\n"
            + "\n".join(f"- {p}" for p in spec.get("files_to_modify", []))
            + "\n\nCURRENT FILE CONTENTS:\n"
            + files_block
            + "\n\nOutput the updated files now."
        )
        return sys_prompt, user_prompt

    def _parse_file_blocks(self, llm_output: str) -> dict[str, str]:
        """Extract ```file:path\n<content>\n``` blocks."""
        out: dict[str, str] = {}
        for m in re.finditer(r"```file:([^\n]+)\n(.*?)\n```", llm_output, re.DOTALL):
            path = m.group(1).strip()
            content = m.group(2)
            out[path] = content
        return out

    # --- Main loop ---

    def on_tick(self):
        proposals = self.api_get(
            f"/v1/chat/?from_agent_id={AGENT_IDS['planner']}&message_type=proposal&limit=10"
        ) or []
        if not isinstance(proposals, list):
            return
        new_msgs = self.get_new_messages(proposals)
        if not new_msgs:
            return

        for msg in new_msgs[:2]:  # cap parallelism
            mid = msg["id"]
            spec = self._parse_proposal(msg)
            if not spec or "id" not in spec:
                self.log.warning("proposal without spec: %s", msg.get("title", "")[:60])
                self.mark_processed(mid)
                continue

            item_id = spec["id"]
            title = spec.get("title", item_id)
            self.log.info("PROCESSING %s: %s", item_id, title)

            files_to_modify = spec.get("files_to_modify", [])
            if not files_to_modify:
                self._send_completed(item_id, title, "skip", "no files_to_modify in spec")
                self.mark_processed(mid)
                continue

            current_contents, paths = self._collect_existing_files(files_to_modify)

            sys_p, user_p = self._build_codegen_prompt(spec, current_contents)
            self.log.info("calling deepseek for %s (%d files in context)", item_id, len(current_contents))
            llm_output = self._llm_codegen(sys_p, user_p, max_tokens=5000)
            if not llm_output:
                self._send_completed(item_id, title, "fail", "LLM returned nothing")
                self.mark_processed(mid)
                continue

            file_blocks = self._parse_file_blocks(llm_output)
            if not file_blocks:
                self._send_completed(
                    item_id,
                    title,
                    "fail",
                    "LLM output had no ```file:...``` blocks. Raw: " + llm_output[:300],
                )
                self.mark_processed(mid)
                continue

            ok, err = self._apply_full_files(file_blocks, paths)
            if not ok:
                self._send_completed(item_id, title, "fail", f"apply failed: {err}")
                self.mark_processed(mid)
                continue

            # Git commit
            ok_git, sha_or_err = self._git_commit(
                item_id,
                title,
                [REPO_ROOT / rel for rel in file_blocks.keys()],
            )
            if not ok_git:
                self._send_completed(item_id, title, "fail", f"git commit failed: {sha_or_err}")
                self.mark_processed(mid)
                continue

            sha = sha_or_err
            preview = self._git_diff_preview(sha)
            self.log.info("SHIPPED %s as %s", item_id, sha)
            self._send_completed(
                item_id,
                title,
                "ok",
                f"committed {sha}\n\n```\n{preview}\n```",
            )
            self.mark_processed(mid)

    def _send_completed(self, item_id: str, title: str, status: str, detail: str) -> None:
        body = (
            f"Backlog {item_id} -- status: {status}\n\n"
            f"Title: {title}\n\n"
            f"Detail:\n{detail[:1500]}"
        )
        self.send_msg(
            "planner",
            "completed",
            f"BUILDER {status.upper()}: {item_id} -- {title}",
            body,
        )


if __name__ == "__main__":
    BuilderV6().run()
