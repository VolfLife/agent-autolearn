"""Tests for evidence-led skill evolution and its mutation safety gates."""
import json
import tempfile
import unittest
from pathlib import Path
import evolution

SKILL = """---
name: sample-skill
description: >-
  Use this for the sample procedure.
created_by: autolearn
---

# Sample skill

Do the safe thing.
"""


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "personas" / "default"
        self.p = {
            "persona": "default", "root": root, "skills": root / "skills",
            "usage": root / "skills" / ".usage.json", "verdicts": root / "verdicts.json",
            "evolution": root / ".skill-evolution.json", "backups": root / "evolution-backups",
        }
        folder = self.p["skills"] / "sample-skill"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(SKILL, encoding="utf-8")
        self.p["usage"].write_text(json.dumps({
            "sample-skill": {
                "created_by": "autolearn", "use_count": 5, "patch_count": 1,
                "state": "active", "pinned": False,
            }
        }), encoding="utf-8")
        self.p["verdicts"].write_text(json.dumps({
            "sample-skill": {
                "verdict": "fail", "fail_count": 2,
                "method": "test-suite", "evidence": "2 tests failed",
            }
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_validation_and_token_estimate(self):
        self.assertTrue(evolution.validate("sample-skill", SKILL)["ok"])
        self.assertEqual(evolution.token_estimate("12345"), 2)

    def test_status_ranks_a_deterministic_failure(self):
        report = evolution.status(self.p)
        self.assertEqual(report["optimization_candidates"], ["sample-skill"])
        self.assertGreater(report["skills"][0]["priority"], 100)

    def test_status_limit_bounds_output(self):
        report = {
            "skills": [{"name": "first"}, {"name": "second"}],
            "optimization_candidates": ["first", "second"],
        }
        limited = evolution.limit_report(report, 1)
        self.assertEqual([row["name"] for row in limited["skills"]], ["first"])
        self.assertEqual(limited["optimization_candidates"], ["first"])
        self.assertEqual(limited["candidate_count"], 2)
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(evolution.limit_report(report, 0)["skills"]), 2)
        with self.assertRaises(ValueError):
            evolution.limit_report(report, -1)

    def test_invalid_candidate_never_changes_or_backs_up(self):
        candidate = Path(self.tmp.name) / "candidate.md"
        candidate.write_text(
            SKILL.replace("name: sample-skill", "name: other"), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            evolution.apply(self.p, "sample-skill", candidate)
        current = self.p["skills"] / "sample-skill" / "SKILL.md"
        self.assertEqual(current.read_text(encoding="utf-8"), SKILL)
        self.assertFalse(self.p["backups"].exists())

    def test_status_ignores_external_symlink_skill_file(self):
        outside = Path(self.tmp.name) / "outside.SKILL.md"
        outside.write_text(SKILL.replace("sample-skill", "outside"), encoding="utf-8")
        linked = self.p["skills"] / "linked-skill"
        linked.mkdir()
        try:
            (linked / "SKILL.md").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this system")
        report = evolution.status(self.p)
        self.assertNotIn("linked-skill", {row["name"] for row in report["skills"]})

    def test_apply_refuses_symlink_backup_root(self):
        outside = Path(self.tmp.name) / "outside-backups"
        outside.mkdir()
        linked = Path(self.tmp.name) / "linked-backups"
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this system")
        self.p["backups"] = linked
        candidate = Path(self.tmp.name) / "candidate.md"
        candidate.write_text(
            SKILL.replace("Do the safe thing.", "Changed."), encoding="utf-8"
        )
        with self.assertRaises(PermissionError):
            evolution.apply(self.p, "sample-skill", candidate)
        self.assertEqual(list(outside.iterdir()), [])

    def test_apply_creates_backup_and_history(self):
        candidate = Path(self.tmp.name) / "candidate.md"
        revised = SKILL.replace("Do the safe thing.", "Check inputs before proceeding.")
        candidate.write_text(revised, encoding="utf-8")
        result = evolution.apply(self.p, "sample-skill", candidate)
        self.assertTrue(Path(result["backup"]).is_file())
        current = self.p["skills"] / "sample-skill" / "SKILL.md"
        self.assertEqual(current.read_text(encoding="utf-8"), revised)
        history = json.loads(self.p["evolution"].read_text(encoding="utf-8"))
        self.assertEqual(history["skills"]["sample-skill"]["history"][0]["action"], "apply")

    def test_restore_backup_is_validated(self):
        candidate = Path(self.tmp.name) / "candidate.md"
        candidate.write_text(
            SKILL.replace("Do the safe thing.", "Change the procedure."), encoding="utf-8"
        )
        result = evolution.apply(self.p, "sample-skill", candidate)
        evolution.restore(self.p, "sample-skill", Path(result["backup"]))
        current = self.p["skills"] / "sample-skill" / "SKILL.md"
        self.assertEqual(current.read_text(encoding="utf-8"), SKILL)

    def test_manual_skills_are_read_only(self):
        data = json.loads(self.p["usage"].read_text(encoding="utf-8"))
        data["sample-skill"]["created_by"] = "tracked-manual"
        self.p["usage"].write_text(json.dumps(data), encoding="utf-8")
        candidate = Path(self.tmp.name) / "candidate.md"
        candidate.write_text(
            SKILL.replace("Do the safe thing.", "Changed."), encoding="utf-8"
        )
        with self.assertRaises(PermissionError):
            evolution.apply(self.p, "sample-skill", candidate)

    def test_pinned_skills_are_read_only(self):
        data = json.loads(self.p["usage"].read_text(encoding="utf-8"))
        data["sample-skill"]["pinned"] = True
        self.p["usage"].write_text(json.dumps(data), encoding="utf-8")
        candidate = Path(self.tmp.name) / "candidate.md"
        candidate.write_text(
            SKILL.replace("Do the safe thing.", "Changed."), encoding="utf-8"
        )
        with self.assertRaises(PermissionError):
            evolution.apply(self.p, "sample-skill", candidate)


if __name__ == "__main__":
    unittest.main()
