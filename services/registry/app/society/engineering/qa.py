"""Independent QA evaluation of a code candidate.

QA never trusts the Builder. The verdict is computed by this module from
observable facts inside the worktree — the QA *agent* only decides to run
the evaluation; it cannot assert PASS. Lessons from the legacy Hermes QA
are encoded here as hard rules:

1. Zero acceptance criteria == FAIL. QA never fabricates criteria.
2. Acceptance tests must already exist in the repository and must NOT be
   among the files the Builder changed (no approving your own tests).
3. Every changed file must be on the spec's allow-list and outside the
   protected patterns (checked again here, independently of the Builder).
4. Changed ``*.py`` files must byte-compile.
5. Acceptance tests run via ``python -m pytest <node ids>`` — an argv
   list, never a shell string — inside the worktree, with a timeout and a
   scrubbed environment (no ``*_PASSWORD``/``*_KEY``/``*_SECRET``/``*TOKEN*``
   variables reach the test process).

The static security scan flags secrets, shell/exec primitives and risky
paths in the diff; findings are attached to the candidate for the Security
Reviewer and *also* fail QA when a secret pattern is found.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from ..config import SocietySettings
from ..risk import assess as assess_risk
from .workspace import Workspace, diff_text, is_protected

logger = logging.getLogger(__name__)
BENCH_PREFIX = "bench:"

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
RISKY_CODE_PATTERNS = (
    re.compile(r"\bsubprocess\.(?:run|Popen|call|check_output)\b"),
    re.compile(r"\bos\.(?:system|popen|exec[lv]p?e?)\b"),
    re.compile(r"\beval\(|\bexec\("),
    re.compile(r"\bpickle\.loads?\b"),
    re.compile(r"shell\s*=\s*True"),
    re.compile(r"\bsocket\.socket\b|\bhttpx\.|\brequests\.(?:get|post)\b|\burllib\.request\b"),
)
RISKY_PATH_RE = re.compile(
    r"(auth|secret|config|payment|wallet|escrow|sandbox|websocket|rate_limit|security|\.github/|Dockerfile|requirements|migrations|init-db)",
    re.IGNORECASE,
)
_SCRUB_ENV_RE = re.compile(r"(PASSWORD|_KEY|SECRET|TOKEN|CREDENTIAL)", re.IGNORECASE)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class QAReport:
    verdict: str  # "pass" | "fail"
    summary: str
    checks: List[Check] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    changed_files: List[str] = field(default_factory=list)
    test_output_tail: str = ""
    attempts: int = 1

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d


def scrubbed_env(ws: Workspace) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SCRUB_ENV_RE.search(k)}
    env.update(
        {
            "PYTHONPATH": str(ws.path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ENVIRONMENT": "development",
            "JAEGER_ENABLED": "false",
            "SOCIETY_RUNTIME_ENABLED": "false",
            "SOCIETY_AUTONOMOUS_CODE_ENABLED": "false",
            "HOME": env.get("HOME", "/tmp"),
        }
    )
    return env


def static_security_scan(ws: Workspace, changed: Sequence[str]) -> List[str]:
    findings: List[str] = []
    for rel in changed:
        if is_protected(rel):
            findings.append(f"never-writable path changed: {rel}")
        if RISKY_PATH_RE.search(rel):
            findings.append(f"risky surface touched: {rel}")
    diff = diff_text(ws)
    for never in assess_risk(list(changed), diff).never_findings:
        findings.append(f"NEVER: {never}")
    added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    for ln in added:
        for pat in SECRET_PATTERNS:
            if pat.search(ln):
                findings.append("possible secret in added line (redacted)")
                break
        for pat in RISKY_CODE_PATTERNS:
            if pat.search(ln):
                findings.append(f"risky code primitive added: {pat.pattern[:40]}")
                break
    # de-duplicate, keep order
    seen = set()
    out = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out[:30]


def _compile_check(ws: Workspace, changed: Sequence[str]) -> Check:
    errors = []
    for rel in changed:
        if rel.endswith(".py"):
            p = ws.path / rel
            if not p.exists():
                continue
            try:
                # compile in-memory: never writes bytecode into the worktree
                compile(p.read_bytes(), str(p), "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as exc:
                errors.append(f"{rel}: {type(exc).__name__}: {str(exc)[:200]}")
    return Check("compile", not errors, "; ".join(errors) if errors else f"{sum(1 for c in changed if c.endswith('.py'))} python file(s) compile")


def _run_acceptance(ws: Workspace, tests: Sequence[str], timeout: int) -> Check:
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", "--timeout", str(min(timeout, 600)), *tests]
    try:
        proc = subprocess.run(argv, cwd=str(ws.path), env=scrubbed_env(ws), capture_output=True, text=True, timeout=timeout + 30, check=False)
    except subprocess.TimeoutExpired:
        return Check("acceptance_tests", False, f"timed out after {timeout}s")
    except OSError as exc:
        return Check("acceptance_tests", False, f"could not start pytest: {exc}")
    tail = (proc.stdout + "\n" + proc.stderr)[-3000:]
    if proc.returncode != 0 and "unrecognized arguments: --timeout" in tail:
        # pytest-timeout not installed in this environment: rerun without it.
        argv = [a for a in argv if a not in ("--timeout", str(min(timeout, 600)))]
        proc = subprocess.run(argv, cwd=str(ws.path), env=scrubbed_env(ws), capture_output=True, text=True, timeout=timeout + 30, check=False)
        tail = (proc.stdout + "\n" + proc.stderr)[-3000:]
    ok = proc.returncode == 0
    return Check("acceptance_tests", ok, tail.strip()[-1500:])


def evaluate_candidate(settings: SocietySettings, ws: Workspace, spec: Dict, changed: Sequence[str], *, attempts: int = 1,
                       bench_runner: Optional[Callable[[str], Dict]] = None) -> QAReport:
    """``bench_runner(task_id)`` runs a ``bench:<task>`` proof (engineering/bench_proof.py) after the
    pytest acceptance tests pass; without one a bench proof fails (never assumed)."""
    checks: List[Check] = []
    failures: List[str] = []
    allowed = {a.replace(os.sep, "/") for a in (spec.get("files_allowed") or [])}
    criteria = [t for t in (spec.get("acceptance_tests") or []) if isinstance(t, str) and t]
    benches = [t[len(BENCH_PREFIX):] for t in criteria if t.startswith(BENCH_PREFIX)]
    tests = [t for t in criteria if not t.startswith(BENCH_PREFIX)]

    # 1. criteria must exist
    if not criteria:
        checks.append(Check("acceptance_criteria_present", False, "spec has no acceptance_tests; QA never fabricates criteria"))
    else:
        checks.append(Check("acceptance_criteria_present", True, f"{len(tests)} acceptance test target(s), {len(benches)} bench proof(s)"))

    # 2. change must be non-empty and on the allow-list
    if not changed:
        checks.append(Check("non_empty_change", False, "candidate contains no changes"))
    else:
        checks.append(Check("non_empty_change", True, f"{len(changed)} file(s) changed"))
    off_list = [c for c in changed if c not in allowed]
    checks.append(Check("allow_list", not off_list, "; ".join(off_list) if off_list else "all changed files are on files_allowed"))
    protected = [c for c in changed if is_protected(c)]
    checks.append(Check("protected_paths", not protected, "; ".join(protected) if protected else "no never-writable path touched"))
    risk = assess_risk(list(changed), diff_text(ws), spec_kind=str(spec.get("kind") or ""))
    checks.append(Check("no_never_findings", not risk.never_findings, "; ".join(risk.never_findings) if risk.never_findings else f"trusted risk tier {risk.tier.value}"))

    # 3. Builder may not modify the tests that judge it
    test_paths = set()
    for t in tests:
        test_paths.add(t.split("::")[0])
    self_judged = [c for c in changed if c in test_paths]
    checks.append(Check("no_self_judging", not self_judged, "; ".join(self_judged) if self_judged else "acceptance tests untouched by Builder"))
    missing_tests = [t for t in test_paths if not (ws.path / t).exists()]
    checks.append(Check("acceptance_tests_exist", not missing_tests, "; ".join(missing_tests) if missing_tests else "acceptance tests present in worktree"))

    # 4. compile
    if spec.get("must_compile", True):
        checks.append(_compile_check(ws, changed))

    # 5. static scan (secrets fail QA outright; other findings go to Security)
    findings = static_security_scan(ws, changed)
    secret_findings = [f for f in findings if "secret" in f]
    checks.append(Check("no_secrets_added", not secret_findings, "; ".join(secret_findings) if secret_findings else "no secret patterns in diff"))

    # 6. run acceptance only if the gates above passed (avoid executing an off-list change)
    gates_ok = all(c.passed for c in checks)
    test_tail = ""
    bench_results: List[Dict] = []
    if gates_ok:
        if tests:
            acc = _run_acceptance(ws, tests, settings.qa_test_timeout_seconds)
            checks.append(acc)
            test_tail = acc.detail
        # 7. a bench proof runs only on a candidate whose pytest acceptance is green (it costs model calls)
        for task_id in benches:
            if not all(c.passed for c in checks):
                checks.append(Check("bench_proof", False, f"bench:{task_id} skipped: acceptance tests failed"))
                continue
            result = bench_runner(task_id) if bench_runner else {"task_id": task_id, "passed": False, "error": "no bench runner in this process"}
            bench_results.append(result)
            checks.append(Check("bench_proof", bool(result.get("passed")), _bench_line(result)))
    else:
        checks.append(Check("acceptance_tests", False, "skipped: pre-flight gates failed"))

    failures = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
    verdict = "pass" if not failures else "fail"
    summary = "PASS: all checks green" if verdict == "pass" else "FAIL: " + "; ".join(f[:160] for f in failures[:4])
    report = QAReport(
        verdict=verdict,
        summary=summary[:1000],
        checks=checks,
        failures=failures,
        changed_files=list(changed),
        test_output_tail=test_tail[-1500:],
        attempts=attempts,
    )
    report_dict = report.to_dict()
    report_dict["static_findings"] = findings
    report.__dict__["static_findings"] = findings  # exposed for the executor
    report.__dict__["risk_tier"] = risk.tier.value
    report.__dict__["bench_proof"] = bench_results
    return report


def _bench_line(r: Dict) -> str:
    from .bench_proof import summary_line  # noqa: PLC0415

    return summary_line(r)
