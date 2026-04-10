from pathlib import Path
from typing import List, Dict, Optional, Tuple
import re
import structlog

logger = structlog.get_logger()


class CodeChunker:
    EXTENSION_LANGUAGE_MAP = {
        ".py": "python",
        ".pyx": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".cpp": "cpp",
        ".c": "c",
        ".h": "c",
        ".hpp": "cpp",
        ".cs": "csharp",
        ".rb": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".kt": "kotlin",
        ".scala": "scala",
        ".sql": "sql",
        ".html": "html",
        ".css": "css",
        ".scss": "scss",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".md": "markdown",
        ".sh": "bash",
        ".bash": "bash",
        ".ps1": "powershell",
    }

    LANGUAGE_PATTERNS = {
        "python": {
            "block_start": r"^(class|def|async def|@)",
            "block_end": r"^\s*$",
        },
        "javascript": {
            "block_start": r"^(function|const|let|var|class|export|import)",
            "block_end": r"^\s*$",
        },
        "typescript": {
            "block_start": r"^(function|const|let|var|class|interface|type|export|import)",
            "block_end": r"^\s*$",
        },
        "java": {
            "block_start": r"^(public|private|protected|class|interface|enum)",
            "block_end": r"^\s*$",
        },
        "go": {
            "block_start": r"^(func|type|package)",
            "block_end": r"^\s*$",
        },
        "rust": {
            "block_start": r"^(pub|fn|struct|enum|impl|trait|use)",
            "block_end": r"^\s*$",
        },
    }

    def __init__(self, max_chunk_size: int = 2000):
        self.max_chunk_size = max_chunk_size

    def get_language(self, file_path: str) -> Optional[str]:
        ext = Path(file_path).suffix.lower()
        return self.EXTENSION_LANGUAGE_MAP.get(ext)

    def chunk_by_language(
        self, content: str, language: Optional[str] = None
    ) -> List[Dict[str, any]]:
        if not language:
            return self.chunk_plain_text(content)

        patterns = self.LANGUAGE_PATTERNS.get(language, {})
        
        if language == "python":
            return self._chunk_python(content)
        elif language in ("javascript", "typescript"):
            return self._chunk_js_ts(content, language)
        elif language in ("java", "go", "rust"):
            return self._chunk_braced(content, patterns)
        else:
            return self.chunk_plain_text(content)

    def _chunk_python(self, content: str) -> List[Dict[str, any]]:
        lines = content.split("\n")
        chunks = []
        current_chunk_lines = []
        current_size = 0
        in_function = False
        in_class = False
        indent_stack = [0]

        for i, line in enumerate(lines):
            stripped = line.strip()
            
            if stripped.startswith("class ") or stripped.startswith("@"):
                if current_chunk_lines:
                    chunks.append(self._create_chunk(current_chunk_lines, current_size))
                    current_chunk_lines = []
                    current_size = 0
            
            if stripped.startswith("def ") or stripped.startswith("async def "):
                if current_chunk_lines and not in_function:
                    chunks.append(self._create_chunk(current_chunk_lines, current_size))
                    current_chunk_lines = []
                    current_size = 0
                in_function = True
            elif stripped and not line.startswith(" ") and not line.startswith("\t"):
                in_function = False
            
            line_size = len(line) + 1
            if current_size + line_size > self.max_chunk_size and current_chunk_lines:
                chunks.append(self._create_chunk(current_chunk_lines, current_size))
                current_chunk_lines = []
                current_size = 0
            
            current_chunk_lines.append(line)
            current_size += line_size

        if current_chunk_lines:
            chunks.append(self._create_chunk(current_chunk_lines, current_size))

        return chunks

    def _chunk_js_ts(self, content: str, language: str) -> List[Dict[str, any]]:
        lines = content.split("\n")
        chunks = []
        current_chunk_lines = []
        current_size = 0
        brace_count = 0

        for line in lines:
            brace_count += line.count("{") - line.count("}")
            line_size = len(line) + 1

            if current_size + line_size > self.max_chunk_size and current_chunk_lines and brace_count == 0:
                chunks.append(self._create_chunk(current_chunk_lines, current_size))
                current_chunk_lines = []
                current_size = 0

            current_chunk_lines.append(line)
            current_size += line_size

        if current_chunk_lines:
            chunks.append(self._create_chunk(current_chunk_lines, current_size))

        return chunks

    def _chunk_braced(
        self, content: str, patterns: Dict[str, str]
    ) -> List[Dict[str, any]]:
        lines = content.split("\n")
        chunks = []
        current_chunk_lines = []
        current_size = 0
        brace_count = 0

        for line in lines:
            brace_count += line.count("{") - line.count("}")
            line_size = len(line) + 1

            if current_size + line_size > self.max_chunk_size and current_chunk_lines and brace_count == 0:
                chunks.append(self._create_chunk(current_chunk_lines, current_size))
                current_chunk_lines = []
                current_size = 0

            current_chunk_lines.append(line)
            current_size += line_size

        if current_chunk_lines:
            chunks.append(self._create_chunk(current_chunk_lines, current_size))

        return chunks

    def chunk_plain_text(self, content: str) -> List[Dict[str, any]]:
        chunks = []
        current_chunk_lines = []
        current_size = 0

        for line in content.split("\n"):
            line_size = len(line) + 1
            if current_size + line_size > self.max_chunk_size and current_chunk_lines:
                chunks.append(self._create_chunk(current_chunk_lines, current_size))
                current_chunk_lines = []
                current_size = 0

            current_chunk_lines.append(line)
            current_size += line_size

        if current_chunk_lines:
            chunks.append(self._create_chunk(current_chunk_lines, current_size))

        return chunks

    def _create_chunk(self, lines: List[str], size: int) -> Dict[str, any]:
        content = "\n".join(lines)
        start_line = 1
        end_line = len(lines)
        
        docstring_match = re.search(r'"""[\s\S]*?"""', content)
        docstring = None
        if docstring_match:
            docstring = docstring_match.group(0).strip()
        
        first_line = lines[0].strip() if lines else ""
        
        return {
            "content": content,
            "size": size,
            "lines": (start_line, end_line),
            "first_line": first_line,
            "docstring": docstring,
        }

    def chunk_file(self, file_path: str, content: Optional[str] = None) -> List[Dict[str, any]]:
        if content is None:
            path = Path(file_path)
            if not path.exists():
                return []
            content = path.read_text(encoding="utf-8")
        
        language = self.get_language(file_path)
        return self.chunk_by_language(content, language)


def chunk_file_by_extension(
    file_path: str, content: Optional[str] = None, max_chunk_size: int = 2000
) -> List[Dict[str, any]]:
    chunker = CodeChunker(max_chunk_size=max_chunk_size)
    return chunker.chunk_file(file_path, content)


def get_language_from_extension(file_path: str) -> Optional[str]:
    chunker = CodeChunker()
    return chunker.get_language(file_path)