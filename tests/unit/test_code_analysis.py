"""Unit tests for code analysis tool."""
import pytest
import tempfile
from pathlib import Path


class TestCodeAnalyzer:
    def test_initialization(self):
        from agent.tools import CodeAnalyzer

        analyzer = CodeAnalyzer()
        assert analyzer.logger is not None

    def test_analyze_simple_file(self):
        from agent.tools import CodeAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("""
def hello():
    '''Says hello.'''
    return "Hello"

class Greeter:
    '''A greeter class.'''
    
    def greet(self, name):
        return f"Hello, {name}"
""")

            analyzer = CodeAnalyzer()
            result = analyzer.analyze_file(str(test_file))

            assert result["success"] == True
            assert len(result["functions"]) == 1
            assert result["functions"][0]["name"] == "hello"
            assert len(result["classes"]) == 1
            assert result["classes"][0]["name"] == "Greeter"

    def test_analyze_async_functions(self):
        from agent.tools import CodeAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "async_test.py"
            test_file.write_text("""
async def async_func():
    pass

def sync_func():
    pass
""")

            analyzer = CodeAnalyzer()
            result = analyzer.analyze_file(str(test_file))

            assert result["success"] == True
            assert len(result["functions"]) == 2
            assert result["functions"][0]["is_async"] == True
            assert result["functions"][1]["is_async"] == False

    def test_analyze_imports(self):
        from agent.tools import CodeAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "imports.py"
            test_file.write_text("""
import os
import sys
from pathlib import Path
from typing import List, Dict
from collections import defaultdict as dd
""")

            analyzer = CodeAnalyzer()
            result = analyzer.analyze_file(str(test_file))

            assert result["success"] == True
            assert len(result["imports"]) >= 4

    def test_analyze_nonexistent_file(self):
        from agent.tools import CodeAnalyzer, CodeAnalysisError

        analyzer = CodeAnalyzer()
        result = analyzer.analyze_file("nonexistent.py")

        assert result["success"] == False

    def test_analyze_non_python_file(self):
        from agent.tools import CodeAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Some text")

            analyzer = CodeAnalyzer()
            result = analyzer.analyze_file(str(test_file))

            assert result["success"] == False

    def test_analyze_syntax_error(self):
        from agent.tools import CodeAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "bad.py"
            test_file.write_text("def bad(:")

            analyzer = CodeAnalyzer()
            result = analyzer.analyze_file(str(test_file))

            assert result["success"] == False
            assert "Syntax error" in result["error"]

    def test_get_function_at_line(self):
        from agent.tools import CodeAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("""
def first():
    pass

def second():
    pass
""")

            analyzer = CodeAnalyzer()
            func = analyzer.get_function_at_line(str(test_file), 3)

            assert func is not None
            assert func.name == "first"

    def test_find_function(self):
        from agent.tools import CodeAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("""
def my_function():
    pass

class MyClass:
    def method(self):
        pass
""")

            analyzer = CodeAnalyzer()
            func = analyzer.find_function(str(test_file), "my_function")

            assert func is not None
            assert func.name == "my_function"

    def test_analyze_directory(self):
        from agent.tools import CodeAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "file1.py").write_text("def func1(): pass")
            Path(tmpdir, "file2.py").write_text("class Class1: pass")

            analyzer = CodeAnalyzer()
            result = analyzer.analyze_directory(tmpdir)

            assert result["success"] == True
            assert result["analyzed_files"] == 2
            assert result["summary"]["total_functions"] >= 1
            assert result["summary"]["total_classes"] >= 1

    def test_extract_method_from_class(self):
        from agent.tools import CodeAnalyzer

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("""
class MyClass:
    def my_method(self, arg1, arg2):
        '''A method.'''
        pass
""")

            analyzer = CodeAnalyzer()
            result = analyzer.analyze_file(str(test_file))

            assert result["success"] == True
            assert len(result["classes"]) == 1
            assert len(result["classes"][0]["methods"]) == 1
            method = result["classes"][0]["methods"][0]
            assert method["name"] == "my_method"
            assert method["args"] == ["self", "arg1", "arg2"]
            assert method["docstring"] == "A method."