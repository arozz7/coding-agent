from pathlib import Path
from typing import List, Dict, Optional, Set
import networkx as nx
import structlog

logger = structlog.get_logger()


class MemoryWiki:
    def __init__(self, project_id: str = "default"):
        self.project_id = project_id
        self.graph = nx.DiGraph()
        self._file_nodes: Dict[str, dict] = {}
        self._function_nodes: Dict[str, dict] = {}
        self._class_nodes: Dict[str, dict] = {}
        self.logger = logger.bind(component="memory_wiki", project_id=project_id)

    def add_file(
        self,
        file_path: str,
        file_type: str = "source",
        language: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        if file_path in self._file_nodes:
            self.logger.debug("file_already_exists", file_path=file_path)
            return

        node_data = {
            "type": "file",
            "file_type": file_type,
            "language": language,
            "metadata": metadata or {},
        }
        
        self.graph.add_node(file_path, **node_data)
        self._file_nodes[file_path] = node_data
        self.logger.debug("file_added", file_path=file_path, language=language)

    def add_function(
        self,
        file_path: str,
        function_name: str,
        signature: str,
        line_start: int,
        line_end: int,
        calls: Optional[List[str]] = None,
        called_by: Optional[List[str]] = None,
    ) -> None:
        node_id = f"{file_path}:{function_name}"
        
        node_data = {
            "type": "function",
            "file_path": file_path,
            "signature": signature,
            "line_start": line_start,
            "line_end": line_end,
            "calls": calls or [],
            "called_by": called_by or [],
        }
        
        self.graph.add_node(node_id, **node_data)
        self._function_nodes[node_id] = node_data
        
        if not self._file_nodes.get(file_path):
            self.add_file(file_path)
        
        self.graph.add_edge(file_path, node_id, relation="contains")
        
        if calls:
            for called_func in calls:
                self.graph.add_edge(node_id, called_func, relation="calls")
        
        if called_by:
            for caller_func in called_by:
                self.graph.add_edge(caller_func, node_id, relation="called_by")
        
        self.logger.debug("function_added", function=function_name, file=file_path)

    def add_class(
        self,
        file_path: str,
        class_name: str,
        line_start: int,
        line_end: int,
        methods: Optional[List[str]] = None,
        base_classes: Optional[List[str]] = None,
    ) -> None:
        node_id = f"{file_path}:{class_name}"
        
        node_data = {
            "type": "class",
            "file_path": file_path,
            "line_start": line_start,
            "line_end": line_end,
            "methods": methods or [],
            "base_classes": base_classes or [],
        }
        
        self.graph.add_node(node_id, **node_data)
        self._class_nodes[node_id] = node_data
        
        if not self._file_nodes.get(file_path):
            self.add_file(file_path)
        
        self.graph.add_edge(file_path, node_id, relation="contains")
        
        if base_classes:
            for base in base_classes:
                self.graph.add_edge(node_id, base, relation="inherits")
        
        self.logger.debug("class_added", class_name=class_name, file=file_path)

    def add_import(self, from_file: str, to_module: str, imported_names: List[str]) -> None:
        node_id = f"{from_file}:import:{to_module}"
        
        node_data = {
            "type": "import",
            "from_file": from_file,
            "to_module": to_module,
            "imported_names": imported_names,
        }
        
        self.graph.add_node(node_id, **node_data)
        self.graph.add_edge(from_file, node_id, relation="has_import")
        self.graph.add_edge(node_id, to_module, relation="imports")

    def get_dependencies(self, file_path: str, max_depth: int = 1) -> List[str]:
        if file_path not in self.graph:
            return []
        
        dependencies = set()
        current_depth = 0
        frontier = {file_path}
        
        while current_depth < max_depth and frontier:
            next_frontier = set()
            for node in frontier:
                successors = list(self.graph.successors(node))
                for succ in successors:
                    if succ not in dependencies:
                        dependencies.add(succ)
                        next_frontier.add(succ)
            frontier = next_frontier
            current_depth += 1
        
        return list(dependencies)

    def get_dependents(self, file_path: str, max_depth: int = 1) -> List[str]:
        if file_path not in self.graph:
            return []
        
        dependents = set()
        current_depth = 0
        frontier = {file_path}
        
        while current_depth < max_depth and frontier:
            next_frontier = set()
            for node in frontier:
                predecessors = list(self.graph.predecessors(node))
                for pred in predecessors:
                    if pred not in dependents:
                        dependents.add(pred)
                        next_frontier.add(pred)
            frontier = next_frontier
            current_depth += 1
        
        return list(dependents)

    def find_function_call_chain(
        self, start_func: str, end_func: str
    ) -> Optional[List[str]]:
        try:
            path = nx.shortest_path(self.graph, start_func, end_func)
            return path
        except (nx.NodeNotFound, nx.NetworkXNoPath):
            return None

    def get_file_imports(self, file_path: str) -> List[dict]:
        imports = []
        
        for node in self.graph.successors(file_path):
            node_data = self.graph.nodes[node]
            if node_data.get("type") == "import":
                imports.append({
                    "module": node_data.get("to_module"),
                    "names": node_data.get("imported_names", []),
                })
        
        return imports

    def get_file_functions(self, file_path: str) -> List[dict]:
        functions = []
        
        for node in self.graph.successors(file_path):
            node_data = self.graph.nodes[node]
            if node_data.get("type") == "function":
                functions.append({
                    "name": node_data.get("signature", "").split("(")[0].strip(),
                    "signature": node_data.get("signature"),
                    "line_start": node_data.get("line_start"),
                    "line_end": node_data.get("line_end"),
                })
        
        return functions

    def get_file_classes(self, file_path: str) -> List[dict]:
        classes = []
        
        for node in self.graph.successors(file_path):
            node_data = self.graph.nodes[node]
            if node_data.get("type") == "class":
                node_id = node.split(":")[-1] if ":" in node else node
                classes.append({
                    "name": node_id,
                    "line_start": node_data.get("line_start"),
                    "line_end": node_data.get("line_end"),
                    "methods": node_data.get("methods", []),
                })
        
        return classes

    def get_impact_analysis(self, file_path: str) -> dict:
        direct_deps = self.get_dependencies(file_path, max_depth=1)
        indirect_deps = self.get_dependencies(file_path, max_depth=2)
        
        direct_dependents = self.get_dependents(file_path, max_depth=1)
        indirect_dependents = self.get_dependents(file_path, max_depth=2)
        
        return {
            "file": file_path,
            "directly_imports": len(direct_deps),
            "directly_imported_by": len(direct_dependents),
            "total_impact_scope": len(indirect_deps) + len(indirect_dependents),
            "risk_level": "high" if len(direct_dependents) > 5 else "medium" if len(direct_dependents) > 2 else "low",
        }

    def get_statistics(self) -> dict:
        return {
            "total_nodes": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "files": len(self._file_nodes),
            "functions": len(self._function_nodes),
            "classes": len(self._class_nodes),
        }

    def export_to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "nodes": dict(self.graph.nodes(data=True)),
            "edges": [(u, v, d) for u, v, d in self.graph.edges(data=True)],
            "statistics": self.get_statistics(),
        }

    def clear(self) -> None:
        self.graph.clear()
        self._file_nodes.clear()
        self._function_nodes.clear()
        self._class_nodes.clear()
        self.logger.info("memory_wiki_cleared", project_id=self.project_id)


def create_memory_wiki(project_id: str = "default") -> MemoryWiki:
    return MemoryWiki(project_id)