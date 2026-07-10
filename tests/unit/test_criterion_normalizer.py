"""Unit tests for normalize_criterion — rewrites/drops criteria that can't
run reliably as a shell command on both Windows and Linux.

Regression coverage for: "command exits 0: find src-tauri/db/ -type f -name
'*.sql' | grep -q ." — a raw Unix pipeline that fails unconditionally on
Windows (cmd.exe's built-in `find` searches file *contents*, not paths) and
is, even on Linux, strictly worse than the pure-Python glob check the
codebase already has for exactly this "does a file matching X exist"
question. See logs/api-20260709-111051.log: this exact criterion sent the
agent down a rabbit hole that created a second, conflicting DB migration
trying to satisfy an unpassable check.
"""
from agent.orchestration.criterion_evaluator import normalize_criterion


class TestNormalizeCriterionFindRewrite:
    def test_find_type_f_name_rewritten_to_file_exists_glob(self):
        result = normalize_criterion(
            "command exits 0: find src-tauri/db/ -type f -name '*.sql' | grep -q ."
        )
        assert result == "file exists: src-tauri/db/**/*.sql"

    def test_find_without_trailing_slash(self):
        result = normalize_criterion("command exits 0: find src -type f -name '*.py'")
        assert result == "file exists: src/**/*.py"

    def test_find_double_quoted_pattern(self):
        result = normalize_criterion('command exits 0: find . -type f -name "*.ts"')
        assert result == "file exists: ./**/*.ts"

    def test_find_case_insensitive(self):
        result = normalize_criterion("command exits 0: FIND src -type f -NAME '*.rs'")
        assert result == "file exists: src/**/*.rs"


class TestNormalizeCriterionDropsUnportableCommands:
    def test_bare_grep_command_dropped(self):
        assert normalize_criterion("command exits 0: grep -r 'TODO' src/") is None

    def test_ls_piped_to_grep_dropped(self):
        assert normalize_criterion("command exits 0: ls src/ | grep app.js") is None

    def test_cat_dropped(self):
        assert normalize_criterion("command exits 0: cat package.json | grep version") is None

    def test_wc_dropped(self):
        assert normalize_criterion("command exits 0: find . -name '*.js' | wc -l") is None


class TestNormalizeCriterionLeavesOthersUnchanged:
    def test_build_command_unchanged(self):
        c = "command exits 0: npm run build"
        assert normalize_criterion(c) == c

    def test_test_command_unchanged(self):
        c = "command exits 0: cargo check --manifest-path src-tauri/Cargo.toml"
        assert normalize_criterion(c) == c

    def test_file_exists_criterion_unchanged(self):
        c = "file exists: src/app.js"
        assert normalize_criterion(c) == c

    def test_file_contains_criterion_unchanged(self):
        c = "file contains: package.json:sqlx"
        assert normalize_criterion(c) == c

    def test_visual_criterion_unchanged(self):
        c = "visual: canvas element shows a moving car"
        assert normalize_criterion(c) == c

    def test_plain_english_criterion_unchanged(self):
        c = "The app renders a score counter on screen"
        assert normalize_criterion(c) == c
