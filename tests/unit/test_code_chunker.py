"""Unit tests for code chunker tool."""
import pytest
import tempfile
from pathlib import Path


class TestCodeChunker:
    def test_initialization(self):
        from agent.tools import CodeChunker

        chunker = CodeChunker(max_chunk_size=1000)
        assert chunker.max_chunk_size == 1000

    def test_get_language_python(self):
        from agent.tools import CodeChunker

        chunker = CodeChunker()
        assert chunker.get_language("test.py") == "python"
        assert chunker.get_language("test.pyx") == "python"

    def test_get_language_javascript(self):
        from agent.tools import CodeChunker

        chunker = CodeChunker()
        assert chunker.get_language("test.js") == "javascript"
        assert chunker.get_language("test.ts") == "typescript"
        assert chunker.get_language("test.tsx") == "typescript"

    def test_get_language_unknown(self):
        from agent.tools import CodeChunker

        chunker = CodeChunker()
        assert chunker.get_language("test.xyz") is None
        assert chunker.get_language("test") is None

    def test_chunk_plain_text(self):
        from agent.tools import CodeChunker

        chunker = CodeChunker(max_chunk_size=50)
        content = "line1\nline2\nline3\nline4\nline5"
        chunks = chunker.chunk_plain_text(content)

        assert len(chunks) > 0
        assert "content" in chunks[0]

    def test_chunk_python_file(self):
        from agent.tools import CodeChunker

        chunker = CodeChunker(max_chunk_size=500)
        content = """
def hello():
    '''Says hello.'''
    return "Hello"

def another():
    '''Another function.'''
    pass
"""
        chunks = chunker._chunk_python(content)

        assert len(chunks) > 0

    def test_chunk_file_reads_content(self):
        from agent.tools import CodeChunker

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("def foo():\n    pass\n")

            chunker = CodeChunker()
            chunks = chunker.chunk_file(str(test_file))

            assert len(chunks) > 0
            assert "def foo" in chunks[0]["content"]

    def test_chunk_file_nonexistent(self):
        from agent.tools import CodeChunker

        chunker = CodeChunker()
        chunks = chunker.chunk_file("nonexistent.py")

        assert chunks == []

    def test_helper_functions(self):
        from agent.tools import chunk_file_by_extension, get_language_from_extension

        assert get_language_from_extension("test.py") == "python"
        assert get_language_from_extension("test.js") == "javascript"

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            test_file.write_text("def test():\n    pass\n")
            chunks = chunk_file_by_extension(str(test_file))
            assert len(chunks) > 0

    def test_multiple_languages(self):
        from agent.tools import CodeChunker

        chunker = CodeChunker(max_chunk_size=200)
        
        python_content = """
def func1():
    pass

def func2():
    pass
"""
        chunks = chunker._chunk_python(python_content)
        assert len(chunks) > 0

        js_content = """
function func1() {
}
function func2() {
}
"""
        chunks = chunker._chunk_js_ts(js_content, "javascript")
        assert len(chunks) > 0

    def test_chunk_includes_metadata(self):
        from agent.tools import CodeChunker

        chunker = CodeChunker(max_chunk_size=500)
        content = "def test_func():\n    '''Docstring.'''\n    pass\n"
        
        chunks = chunker.chunk_by_language(content, "python")
        
        assert len(chunks) > 0
        assert "content" in chunks[0]
        assert "size" in chunks[0]
        assert "first_line" in chunks[0]