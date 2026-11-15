#!/usr/bin/env python3
"""Evidence-led, token-aware, human-gated evolution of autolearn-created skills.

This helper intentionally reuses .usage.json and falsify's verdicts.json.
It does not infer per-skill success from a whole session's outcome: correlation
is not causation, especially when several skills were loaded in one session.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_DESCRIPTION_CHARS = 500
RECOMMENDED_BODY_CHARS = 12_000
MAX_SKILL_BYTES = 100_000
BACKUPS_TO_KEEP = 10


def paths(persona: str | None = None) -> dict:
    home = Path(os.environ.get("AUTOLEARN_HOME", Path.home() / ".autolearn")).expanduser()
    if not persona:
        try:
            persona = (home / ".default_persona").read_text(encoding="utf-8").strip()
        except OSError:
            persona = ""
    persona = persona or "default"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", persona):
        raise ValueError("Invalid persona name")
    root = home / "personas" / persona
    return {"persona": persona, "root": root, "skills": root / "skills",
            "usage": root / "skills" / ".usage.json", "verdicts": root / "verdicts.json",
            "evolution": root / ".skill-evolution.json", "backups": root / "evolution-backups"}


def safe_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", name):
        raise ValueError("Skill name must be lowercase-hyphenated")
    return name


def read_json(file: Path, fallback):
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return fallback


def atomic_write(file: Path, content: str) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=file.name + ".", suffix=".tmp", dir=file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, file)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def atomic_json(file: Path, value) -> None:
    atomic_write(file, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def parse_frontmatter(content: str) -> tuple[dict, str]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", content, re.S)
    if not match:
        raise ValueError("Missing or malformed YAML frontmatter")
    lines, fields, i = match.group(1).splitlines(), {}, 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2).strip()
        if value in ("|", "|-", ">", ">-"):
            block = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                block.append(lines[i].strip())
                i += 1
            fields[key] = (" " if value.startswith(">") else "\n").join(x for x in block if x).strip()
        else:
            fields[key] = value.strip("\"'")
            i += 1
    return fields, content[match.end():]


def token_estimate(text: str) -> int:
    return math.ceil(len(text) / 4) if text else 0  # heuristic, not a tokenizer


def validate(name: str, content: str) -> dict:
    errors, warnings = [], []
    try:
        meta, body = parse_frontmatter(content)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": []}
    if meta.get("name", "").strip() != name:
        errors.append(f"frontmatter name must be '{name}'")
    desc = meta.get("description", "").strip()
    if not desc:
        errors.append("frontmatter description is required")
    elif len(desc) > MAX_DESCRIPTION_CHARS:
        warnings.append(f"description is {len(desc)} chars; recommended maximum is {MAX_DESCRIPTION_CHARS}")
    if not body.strip():
        errors.append("skill body is empty")
    if len(body) > RECOMMENDED_BODY_CHARS:
        warnings.append(f"body is {len(body)} chars; move bulky material into references/ (recommended maximum {RECOMMENDED_BODY_CHARS})")
    size = len(content.encode("utf-8"))
    if size > MAX_SKILL_BYTES:
        errors.append(f"file exceeds hard limit ({size} > {MAX_SKILL_BYTES} bytes)")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "description_chars": len(desc), "body_chars": len(body),
            "total_chars": len(content), "estimated_tokens": token_estimate(content),
            "description_estimated_tokens": token_estimate(desc),
            "body_estimated_tokens": token_estimate(body)}


def skill_file(p: dict, name: str) -> Path:
    name = safe_name(name)
    root = p["skills"].resolve()
    file = p["skills"] / name / "SKILL.md"
    if not file.is_file():
        raise FileNotFoundError(f"Skill not found: {name}")
    if not file.resolve().is_relative_to(root):
        raise PermissionError("Skill path resolves outside the persona skill directory")
    return file


def status(p: dict) -> dict:
    usage, verdicts = read_json(p["usage"], {}), read_json(p["verdicts"], {})
    history = read_json(p["evolution"], {"skills": {}}).get("skills", {})
    rows = []
    skill_root = p["skills"].resolve()
    if p["skills"].is_dir():
        for folder in sorted(p["skills"].iterdir()):
            if not folder.is_dir() or folder.is_symlink() or folder.name.startswith("."):
                continue
            file = folder / "SKILL.md"
            if not file.is_file() or file.is_symlink():
                continue
            try:
                if not file.resolve().is_relative_to(skill_root):
                    continue
                content = file.read_text(encoding="utf-8")
                meta, body = parse_frontmatter(content)
            except (OSError, ValueError):
                content, meta, body = "", {}, ""
            u, v = usage.get(folder.name, {}), verdicts.get(folder.name, {})
            uses, fails = int(u.get("use_count", 0) or 0), int(v.get("fail_count", 0) or 0)
            verdict, desc = v.get("verdict", "unverified"), meta.get("description", "")
            cost = token_estimate(content)
            priority, reasons = 0.0, []
            if verdict == "fail":
                priority += 100 + min(fails, 20) * 10
                reasons.append(f"deterministic verification failed ({fails} consecutive failure(s))")
            if uses >= 3 and token_estimate(body) >= 1500:
                priority += min(uses, 20) * 0.5 + min(token_estimate(body) / 1000, 10)
                reasons.append("frequently used and large; review trigger precision and progressive disclosure")
            if len(desc) > MAX_DESCRIPTION_CHARS:
                priority += 5
                reasons.append("long description adds discovery-context cost")
            if verdict == "unverified" and uses >= 3:
                reasons.append("repeatedly used but has no deterministic verification claim")
            if not reasons:
                reasons.append("no strong optimization signal; keep observing")
            hist = history.get(folder.name, {}).get("history", [])
            rows.append({"name": folder.name, "created_by": u.get("created_by", "unknown"),
                "state": u.get("state", "unknown"), "pinned": bool(u.get("pinned")),
                "use_count": uses, "patch_count": int(u.get("patch_count", 0) or 0),
                "verification": {"verdict": verdict, "method": v.get("method"),
                    "fail_count": fails, "checked_at": v.get("checked_at"), "evidence": v.get("evidence")},
                "size": {"chars": len(content), "bytes": len(content.encode("utf-8")),
                    "estimated_tokens": cost, "description_chars": len(desc),
                    "description_estimated_tokens": token_estimate(desc),
                    "body_chars": len(body), "body_estimated_tokens": token_estimate(body)},
                "last_evolution": hist[-1] if hist else None,
                "priority": round(priority, 2), "reasons": reasons,
                "mutable_by_autolearn": u.get("created_by") == "autolearn" and not bool(u.get("pinned"))})
    rows.sort(key=lambda r: (-r["priority"], -r["use_count"], r["name"]))
    return {"persona": p["persona"], "total_skills": len(rows), "skills": rows,
        "optimization_candidates": [r["name"] for r in rows if r["priority"] > 0 and r["mutable_by_autolearn"]],
        "note": "Token counts are rough characters/4 estimates. Session-wide errors are not attributed to individual skills; use deterministic tests and reviewer evidence."}



def limit_report(report: dict, limit: int) -> dict:
    """Return a bounded report for token-efficient agent inspection.

    The underlying ranking still examines the whole library; only the returned
    rows are limited. A limit of zero means no truncation.
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")
    result = dict(report)
    all_rows = report.get("skills", [])
    all_candidates = report.get("optimization_candidates", [])
    result["candidate_count"] = len(all_candidates)
    if limit:
        result["skills"] = all_rows[:limit]
        visible = {row["name"] for row in result["skills"]}
        result["optimization_candidates"] = [
            name for name in all_candidates if name in visible
        ]
    result["truncated"] = limit > 0 and len(all_rows) > limit
    return result



def inspect(p: dict, name: str) -> dict:
    file, report = skill_file(p, name), status(p)
    row = next((r for r in report["skills"] if r["name"] == name), None)
    content = file.read_text(encoding="utf-8")
    return {"skill": row, "path": str(file), "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "validation": validate(name, content), "current_skill": content,
        "procedure": ["Read the skill's wiki pattern and relevant session traces.",
          "Prefer deterministic falsification evidence; treat session-level correlation as inconclusive, not causal.",
          "Draft up to three distinct candidates: improve failure coverage, improve trigger precision, or reduce token cost without losing coverage.",
          "Score the original and candidates on failure coverage, trigger precision, clarity/actionability, and estimated token cost.",
          "Choose a Pareto-best candidate; no change is correct when the original is already best.",
          "Show a unified diff and wait for explicit user approval.",
          "After approval, use evolution.py apply to back up, validate, and atomically replace the skill."]}


def require_owned(p: dict, name: str) -> tuple[Path, str]:
    file = skill_file(p, name)
    meta = read_json(p["usage"], {}).get(name, {})
    if meta.get("created_by") != "autolearn" or meta.get("pinned"):
        raise PermissionError("Only unpinned, persona-local skills created by autolearn may be changed")
    return file, file.read_text(encoding="utf-8")


def backup(p: dict, name: str, content: str) -> Path:
    base = p["backups"]
    if base.is_symlink():
        raise PermissionError("Backup root must not be a symlink")
    base.mkdir(parents=True, exist_ok=True)
    root = base.resolve()
    folder = base / safe_name(name)
    if folder.is_symlink():
        raise PermissionError("Skill backup directory must not be a symlink")
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(root):
        raise PermissionError("Skill backup directory resolves outside the backup root")
    digest = hashlib.sha256(content.encode()).hexdigest()[:12]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target, n = folder / f"{stamp}-{digest}.SKILL.md", 1
    while target.exists():
        target, n = folder / f"{stamp}-{digest}-{n}.SKILL.md", n + 1
    atomic_write(target, content)
    old = sorted(folder.glob("*.SKILL.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    for file in old[BACKUPS_TO_KEEP:]:
        try: file.unlink()
        except OSError: pass
    return target


def log_change(p: dict, name: str, action: str, old: str, new: str, saved: Path) -> dict:
    data = read_json(p["evolution"], {"version": 1, "skills": {}})
    rec = data.setdefault("skills", {}).setdefault(name, {"history": []})
    event = {"at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "action": action, "old_sha256": hashlib.sha256(old.encode()).hexdigest(),
        "new_sha256": hashlib.sha256(new.encode()).hexdigest(), "old_chars": len(old),
        "new_chars": len(new), "old_estimated_tokens": token_estimate(old),
        "new_estimated_tokens": token_estimate(new), "backup": str(saved)}
    rec.setdefault("history", []).append(event)
    rec["history"] = rec["history"][-50:]
    rec["last_evolved_at"], rec["current_sha256"] = event["at"], event["new_sha256"]
    atomic_json(p["evolution"], data)
    return event


def apply(p: dict, name: str, candidate_path: Path) -> dict:
    file, old = require_owned(p, name)
    candidate_path = candidate_path.expanduser().resolve()
    if not candidate_path.is_file():
        raise FileNotFoundError(candidate_path)
    new = candidate_path.read_text(encoding="utf-8")
    checked = validate(name, new)
    if not checked["ok"]:
        raise ValueError("Candidate rejected: " + "; ".join(checked["errors"]))
    if old == new:
        raise ValueError("Candidate is identical to the current skill")
    saved = backup(p, name, old)
    try:
        atomic_write(file, new)
        persisted = file.read_text(encoding="utf-8")
        if not validate(name, persisted)["ok"]:
            raise ValueError("Post-write validation failed")
    except Exception:
        try: atomic_write(file, old)
        except Exception as rollback:
            raise RuntimeError(f"Apply failed; automatic rollback failed. Original backup retained at {saved}: {rollback}")
        raise
    event = log_change(p, name, "apply", old, new, saved)
    return {"ok": True, "skill": name, "backup": str(saved), "validation": checked, "evolution": event}


def restore(p: dict, name: str, backup_path: Path) -> dict:
    file, old = require_owned(p, name)
    base = p["backups"]
    folder = base / safe_name(name)
    if base.is_symlink() or folder.is_symlink():
        raise PermissionError("Backup path components must not be symlinks")
    root = folder.resolve()
    if not root.is_relative_to(base.resolve()):
        raise PermissionError("Skill backup directory resolves outside the backup root")
    source = backup_path.expanduser().resolve()
    if not source.is_relative_to(root) or not source.is_file() or not source.name.endswith(".SKILL.md"):
        raise PermissionError("Backup must be an existing .SKILL.md inside this skill's evolution-backups directory")
    new = source.read_text(encoding="utf-8")
    checked = validate(name, new)
    if not checked["ok"]:
        raise ValueError("Backup rejected: " + "; ".join(checked["errors"]))
    if old == new:
        return {"ok": True, "unchanged": True, "skill": name}
    saved = backup(p, name, old)
    atomic_write(file, new)
    return {"ok": True, "skill": name, "restored_from": str(source),
            "safety_backup": str(saved), "evolution": log_change(p, name, "restore", old, new, saved)}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(errors="replace")
        except (AttributeError, OSError): pass
    parser = argparse.ArgumentParser(description="Evidence-led skill evolution")
    parser.add_argument("--persona", help="Persona name (defaults to active persona)")
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="Rank skill improvement candidates")
    st.add_argument("--json", action="store_true")
    st.add_argument("--limit", type=int, default=20, help="Maximum skill rows to return (0 = all)")
    ins = sub.add_parser("inspect", help="Build an evidence dossier for a skill")
    ins.add_argument("name")
    val = sub.add_parser("validate", help="Validate a candidate without changing files")
    val.add_argument("name"); val.add_argument("candidate", type=Path)
    app = sub.add_parser("apply", help="Back up and apply a validated candidate")
    app.add_argument("name"); app.add_argument("candidate", type=Path)
    rst = sub.add_parser("restore", help="Restore one retained backup")
    rst.add_argument("name"); rst.add_argument("backup", type=Path)
    args = parser.parse_args(argv)
    try:
        p = paths(args.persona)
        if args.command == "status":
            data = limit_report(status(p), args.limit)
            if args.json: print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(f"Persona: {p['persona']} | Skills: {data['total_skills']}")
                for row in data["skills"]:
                    print(f"{row['name']:<32} uses={row['use_count']:<4} verify={row['verification']['verdict']:<12} ~{row['size']['estimated_tokens']} tokens priority={row['priority']}")
                    if row["priority"]: print("  - " + "; ".join(row["reasons"]))
                print("Token estimates are approximate (characters / 4), not tokenizer measurements.")
        elif args.command == "inspect":
            print(json.dumps(inspect(p, args.name), ensure_ascii=False, indent=2))
        elif args.command == "validate":
            result = validate(safe_name(args.name), args.candidate.expanduser().resolve().read_text(encoding="utf-8"))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 2
        elif args.command == "apply":
            print(json.dumps(apply(p, args.name, args.candidate), ensure_ascii=False, indent=2))
        elif args.command == "restore":
            print(json.dumps(restore(p, args.name, args.backup), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, PermissionError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
