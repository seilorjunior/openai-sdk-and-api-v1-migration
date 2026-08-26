#!/usr/bin/env python3
"""Scan repositories for legacy Azure OpenAI client and endpoint usage."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_IGNORES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "node_modules",
    "site-packages",
}
TEXT_SUFFIXES = {
    ".cs",
    ".env",
    ".go",
    ".java",
    ".js",
    ".json",
    ".md",
    ".py",
    ".ps1",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_FILENAMES = {
    "Pipfile",
    "package.json",
    "packages.lock.json",
    "poetry.lock",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
}
RULES = (
    ("AOAI001", "Legacy azure-ai-inference dependency", re.compile(r"\bazure-ai-inference\b", re.IGNORECASE)),
    ("AOAI002", "Legacy ChatCompletionsClient usage", re.compile(r"\bChatCompletionsClient\b")),
    ("AOAI003", "Legacy /models endpoint", re.compile(r"(?<![\w-])/models(?:[/?\"']|$)", re.IGNORECASE)),
    ("AOAI004", "Dated Azure OpenAI api-version", re.compile(r"\bapi-version\s*[=:]\s*[\"']?20\d{2}-\d{2}-\d{2}(?:-preview)?", re.IGNORECASE)),
    ("AOAI005", "Legacy AzureOpenAI client usage", re.compile(r"\bAzureOpenAI\s*\(")),
    ("AOAI006", "Legacy /openai/deployments endpoint", re.compile(r"/openai/deployments/", re.IGNORECASE)),
)


@dataclass(frozen=True)
class Finding:
    rule_id: str
    message: str
    path: str
    line: int
    column: int
    excerpt: str


def iter_files(root: Path, ignored_names: set[str]) -> Iterable[Path]:
    if root.is_file():
        if root.suffix.lower() in TEXT_SUFFIXES or root.name in TEXT_FILENAMES:
            yield root
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_FILENAMES:
            continue
        if any(part in ignored_names for part in path.parts):
            continue
        yield path


def scan_file(path: Path, display_root: Path) -> list[Finding]:
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return []
    try:
        display_path = path.relative_to(display_root).as_posix()
    except ValueError:
        display_path = path.as_posix()
    return scan_text(source, display_path)


def scan_text(source: str, display_path: str) -> list[Finding]:
    findings = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        for rule_id, message, pattern in RULES:
            for match in pattern.finditer(line):
                findings.append(
                    Finding(
                        rule_id=rule_id,
                        message=message,
                        path=display_path,
                        line=line_number,
                        column=match.start() + 1,
                        excerpt=line.strip()[:240],
                    )
                )
    return findings


def scan(roots: Iterable[Path], excludes: Iterable[str] = ()) -> list[Finding]:
    findings = []
    ignored = DEFAULT_IGNORES | set(excludes)
    for supplied_root in roots:
        root = supplied_root.resolve()
        display_root = root if root.is_dir() else root.parent
        for path in iter_files(root, ignored):
            findings.extend(scan_file(path, display_root))
    return sorted(findings, key=lambda item: (item.path, item.line, item.column, item.rule_id))


def json_report(findings: list[Finding]) -> dict[str, object]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
    return {
        "finding_count": len(findings),
        "counts_by_rule": counts,
        "findings": [asdict(finding) for finding in findings],
    }


def sarif_report(findings: list[Finding]) -> dict[str, object]:
    rules = [
        {"id": rule_id, "shortDescription": {"text": message}}
        for rule_id, message, _ in RULES
    ]
    results = [
        {
            "ruleId": finding.rule_id,
            "level": "warning",
            "message": {"text": finding.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": finding.path},
                        "region": {
                            "startLine": finding.line,
                            "startColumn": finding.column,
                            "snippet": {"text": finding.excerpt},
                        },
                    }
                }
            ],
        }
        for finding in findings
    ]
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "openai-v1-migration-scan", "rules": rules}}, "results": results}],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--exclude", action="append", default=[], help="Directory name to ignore; repeatable.")
    parser.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-findings", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing = [str(root) for root in args.roots if not root.exists()]
    if missing:
        print(json.dumps({"error": "roots_not_found", "roots": missing}), file=sys.stderr)
        return 2
    findings = scan(args.roots, args.exclude)
    if args.format == "json":
        rendered = json.dumps(json_report(findings), indent=2, sort_keys=True)
    elif args.format == "sarif":
        rendered = json.dumps(sarif_report(findings), indent=2, sort_keys=True)
    else:
        entries = [
            f"{finding.path}:{finding.line}:{finding.column}: {finding.rule_id} {finding.message}"
            for finding in findings
        ]
        rendered = "\n".join(entries + [f"Found {len(findings)} legacy migration signal(s)."])
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if findings and args.fail_on_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())