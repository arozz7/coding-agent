from pathlib import Path
from typing import List, Optional
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

    def _chunk_code(self, content: str, chunk_size: int = 2000) -> List[str]:
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

    def _compute_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def index_file(
        self, file_path: str, content: str, project_id: str
    ) -> None:
        chunks = self._chunk_code(content)

        ids = [f"{file_path}:chunk:{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "file_path": file_path,
                "project_id": project_id,
                "chunk_type": "file",
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
