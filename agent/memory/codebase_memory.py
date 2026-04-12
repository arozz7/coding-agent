from pathlib import Path
from typing import List, Optional, Dict
import hashlib
import re
import chromadb
import structlog

logger = structlog.get_logger()


class CodebaseMemory:
    def __init__(self, persist_path: str = "data/chroma_db"):
        self.persist_path = Path(persist_path)
        self.persist_path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(self.persist_path))

        self.files_collection = self.client.get_or_create_collection(
            name="code_files"
        )

        self.functions_collection = self.client.get_or_create_collection(
            name="functions"
        )
        self.logger = logger.bind(component="codebase_memory")
        
        # Language-specific patterns for code-aware chunking
        self.language_patterns = {
            "python": {
                "class": r"^class\s+\w+.*?:",
                "function": r"^def\s+\w+.*?:",
                "async_function": r"^async\s+def\s+\w+.*?:",
                "import": r"^(?:from\s+.*?\s+)?import\s+",
            },
            "javascript": {
                "class": r"^(?:export\s+)?class\s+\w+",
                "function": r"^(?:export\s+)?function\s+\w+",
                "arrow": r"^\w+\s*=\s*(?:async\s)?\(",
                "const": r"^(?:export\s+)?const\s+\w+\s*=",
                "import": r"^import\s+",
            },
            "typescript": {
                "class": r"^(?:export\s+)?class\s+\w+",
                "function": r"^(?:export\s+)?(?:async\s+)?function\s+\w+",
                "interface": r"^interface\s+\w+",
                "type": r"^type\s+\w+",
                "import": r"^import\s+",
            },
            "rust": {
                "struct": r"^struct\s+\w+",
                "impl": r"^impl\s+",
                "fn": r"^fn\s+\w+",
                "async_fn": r"^async\s+fn\s+\w+",
                "use": r"^use\s+",
            },
            "go": {
                "func": r"^func\s+",
                "type": r"^type\s+\w+\s+struct",
                "import": r"^import\s+",
            },
        }
    
    def _detect_language(self, file_path: str) -> Optional[str]:
        """Detect programming language from file extension."""
        ext_map = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".rs": "rust",
            ".go": "go",
            ".java": "java",
            ".c": "c",
            ".cpp": "cpp",
            ".cs": "csharp",
        }
        ext = Path(file_path).suffix.lower()
        return ext_map.get(ext)
    
    def _chunk_code_aware(self, content: str, file_path: str) -> List[str]:
        """Split code respecting function/class boundaries."""
        language = self._detect_language(file_path)
        patterns = self.language_patterns.get(language, {})
        
        if not patterns:
            return self._chunk_code_simple(content)
        
        lines = content.split("\n")
        chunks = []
        current_chunk_lines = []
        current_size = 0
        chunk_size = 1500
        
        # Track structure elements for context
        structure_context = []
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            # Check for structure elements
            for struct_type, pattern in patterns.items():
                if re.match(pattern, stripped):
                    structure_context.append(f"[{struct_type}] {stripped[:60]}")
                    break
            
            line_size = len(line)
            
            # If line is too long or chunk is full, start new chunk
            if current_size + line_size > chunk_size and current_chunk_lines:
                # Don't split mid-function - look ahead
                if i < len(lines) - 1:
                    next_line = lines[i + 1].strip()
                    is_structure = False
                    for pattern in patterns.values():
                        if re.match(pattern, next_line):
                            is_structure = True
                            break
                    
                    if not is_structure and any(p in line for p in ["def ", "function ", "class ", "fn "]):
                        current_chunk_lines.append(line)
                        current_size += line_size
                        continue
                
                # Add context header if we have structure info
                chunk_text = "\n".join(current_chunk_lines)
                if structure_context:
                    context_header = "\n# Context: " + " | ".join(structure_context[-3:])
                    chunk_text = context_header + "\n" + chunk_text
                
                chunks.append(chunk_text)
                current_chunk_lines = []
                current_size = 0
                structure_context = structure_context[-3:] if len(structure_context) > 3 else structure_context
            
            current_chunk_lines.append(line)
            current_size += line_size
        
        if current_chunk_lines:
            chunk_text = "\n".join(current_chunk_lines)
            if structure_context:
                context_header = "\n# Context: " + " | ".join(structure_context[-3:])
                chunk_text = context_header + "\n" + chunk_text
            chunks.append(chunk_text)
        
        return chunks if chunks else [content]
    
    def _chunk_code_simple(self, content: str, chunk_size: int = 1500) -> List[str]:
        """Simple line-based chunking without structure awareness."""
        lines = content.split("\n")
        chunks = []
        current_chunk = []
        current_size = 0

        for line in lines:
            line_size = len(line)
            if current_size + line_size > chunk_size and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_size = 0
            current_chunk.append(line)
            current_size += line_size

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks

    def _chunk_code(self, content: str, chunk_size: int = 2000) -> List[str]:
        """Legacy simple chunking method."""
        return self._chunk_code_simple(content, chunk_size)

    def _compute_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def index_file(
        self, file_path: str, content: str, project_id: str
    ) -> None:
        # Use code-aware chunking to respect function/class boundaries
        chunks = self._chunk_code_aware(content, file_path)

        ids = [f"{file_path}:chunk:{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "file_path": file_path,
                "project_id": project_id,
                "chunk_type": "file",
                "language": self._detect_language(file_path) or "unknown",
            }
            for _ in chunks
        ]

        self.files_collection.add(
            documents=chunks,
            metadatas=metadatas,
            ids=ids,
        )
        self.logger.info(
            "file_indexed",
            file_path=file_path,
            chunks=len(chunks),
        )

    def index_function(
        self,
        file_path: str,
        function_name: str,
        signature: str,
        docstring: str,
        project_id: str,
    ) -> None:
        self.functions_collection.add(
            documents=[f"{signature}\n{docstring}"],
            metadatas=[
                {
                    "file_path": file_path,
                    "function_name": function_name,
                    "project_id": project_id,
                    "chunk_type": "function",
                }
            ],
            ids=[f"{file_path}:{function_name}"],
        )

    def search_files(
        self, query: str, n_results: int = 5
    ) -> List[dict]:
        results = self.files_collection.query(
            query_texts=[query],
            n_results=n_results,
            include=["metadatas", "distances"],
        )

        if not results["documents"]:
            return []

        return self._format_results(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )

    def search_functions(
        self, query: str, n_results: int = 5
    ) -> List[dict]:
        results = self.functions_collection.query(
            query_texts=[query],
            n_results=n_results,
            include=["metadatas", "distances"],
        )

        if not results["documents"]:
            return []

        return self._format_results(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )

    def _format_results(
        self,
        documents: List[str],
        metadatas: List[dict],
        distances: List[float],
    ) -> List[dict]:
        formatted = []
        for i in range(len(documents)):
            metadata = metadatas[i]
            distance = distances[i]

            formatted.append(
                {
                    "type": metadata["chunk_type"],
                    "path": metadata["file_path"],
                    "content": documents[i],
                    "relevance_score": round(1 - distance, 3),
                    "metadata": metadata,
                }
            )

        return formatted

    def clear_project(self, project_id: str) -> None:
        try:
            self.files_collection.delete(where={"project_id": project_id})
            self.functions_collection.delete(where={"project_id": project_id})
            self.logger.info("project_cleared", project_id=project_id)
        except Exception as e:
            self.logger.error(
                "clear_project_error", project_id=project_id, error=str(e)
            )
    
    def index_workspace(self, workspace_path: str, project_id: str = "default") -> Dict[str, int]:
        """Index all code files in a workspace.
        
        Args:
            workspace_path: Path to the workspace directory
            project_id: Project identifier for grouping
        
        Returns:
            Summary of indexed files
        """
        workspace = Path(workspace_path)
        if not workspace.exists():
            self.logger.warning("workspace_not_found", path=str(workspace))
            return {"indexed": 0, "errors": 0}
        
        # File extensions to index
        code_extensions = {".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go", ".java", ".c", ".cpp", ".cs", ".rb", ".php"}
        
        # Directories to skip
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "target", "bin", "obj"}
        
        indexed_count = 0
        error_count = 0
        
        for file_path in workspace.rglob("*"):
            try:
                if file_path.is_file() and file_path.suffix.lower() in code_extensions:
                    # Skip files in skipped directories
                    if any(skip in file_path.parts for skip in skip_dirs):
                        continue
                    
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    if content.strip():
                        self.index_file(str(file_path.relative_to(workspace)), content, project_id)
                        indexed_count += 1
            except Exception as e:
                self.logger.warning("file_index_error", path=str(file_path), error=str(e))
                error_count += 1
        
        self.logger.info("workspace_indexed", indexed=indexed_count, errors=error_count)
        return {"indexed": indexed_count, "errors": error_count}
    
    def get_relevant_context(self, task: str, project_id: str = "default", max_chunks: int = 5) -> str:
        """Get relevant code context for a task using RAG.
        
        Args:
            task: The task or question requiring code context
            project_id: Project identifier
            max_chunks: Maximum number of code chunks to retrieve
        
        Returns:
            Formatted context string for agent prompts
        """
        results = self.search_files(task, n_results=max_chunks)
        
        if not results:
            return ""
        
        context_parts = ["## Relevant Code Context\n"]
        
        for i, result in enumerate(results):
            path = result.get("path", "unknown")
            content = result.get("content", "")
            score = result.get("relevance_score", 0)
            
            context_parts.append(f"### {path} (relevance: {score})\n```\n{content[:800]}\n```\n")
        
        return "\n".join(context_parts)
    
    def find_functions(self, query: str, project_id: str = "default", n_results: int = 10) -> List[dict]:
        """Search for functions/methods by name or signature."""
        results = self.functions_collection.query(
            query_texts=[query],
            n_results=n_results,
            where={"project_id": project_id},
            include=["metadatas", "documents", "distances"],
        )
        
        if not results["documents"]:
            return []
        
        return self._format_results(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )
    
    def get_file_summary(self, file_path: str, project_id: str = "default") -> dict:
        """Get a summary of all chunks for a specific file."""
        results = self.files_collection.get(
            where={
                "file_path": file_path,
                "project_id": project_id,
            },
            include=["metadatas", "documents"],
        )
        
        if not results.get("documents"):
            return {"exists": False, "chunks": 0}
        
        return {
            "exists": True,
            "chunks": len(results["documents"]),
            "language": results["metadatas"][0].get("language", "unknown") if results["metadatas"] else "unknown",
        }
    
    def get_stats(self) -> dict:
        """Get statistics about the vector store."""
        try:
            file_count = self.files_collection.count()
            func_count = self.functions_collection.count()
            return {
                "total_chunks": file_count,
                "total_functions": func_count,
            }
        except Exception as e:
            self.logger.error("stats_error", error=str(e))
            return {"error": str(e)}
