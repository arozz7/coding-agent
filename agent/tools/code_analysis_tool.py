import ast
from pathlib import Path
from typing import List, Dict, Optional, Set
import structlog

logger = structlog.get_logger()


class CodeAnalysisError(Exception):
    pass


class FunctionInfo:
    def __init__(self, name: str, line_start: int, line_end: int, args: List[str], docstring: Optional[str], is_async: bool = False):
        self.name = name
        self.line_start = line_start
        self.line_end = line_end
        self.args = args
        self.docstring = docstring
        self.is_async = is_async
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "args": self.args,
            "docstring": self.docstring,
            "is_async": self.is_async,
        }


class ClassInfo:
    def __init__(self, name: str, line_start: int, line_end: int, docstring: Optional[str], methods: List[FunctionInfo]):
        self.name = name
        self.line_start = line_start
        self.line_end = line_end
        self.docstring = docstring
        self.methods = methods
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "docstring": self.docstring,
            "methods": [m.to_dict() for m in self.methods],
        }


class ImportInfo:
    def __init__(self, module: str, names: List[str], is_from: bool, alias: Optional[str] = None):
        self.module = module
        self.names = names
        self.is_from = is_from
        self.alias = alias
    
    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "names": self.names,
            "is_from": self.is_from,
            "alias": self.alias,
        }


class CodeAnalyzer:
    def __init__(self):
        self.logger = logger.bind(component="code_analyzer")

    def _get_docstring(self, node: ast.AST) -> Optional[str]:
        docstring = ast.get_docstring(node)
        return docstring.strip() if docstring else None

    def _extract_function_info(self, node: ast.FunctionDef) -> FunctionInfo:
        args = [arg.arg for arg in node.args.args]
        return FunctionInfo(
            name=node.name,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            args=args,
            docstring=self._get_docstring(node),
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )

    def _extract_class_info(self, node: ast.ClassDef) -> ClassInfo:
        methods = []
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                methods.append(self._extract_function_info(item))
            elif isinstance(item, ast.AsyncFunctionDef):
                methods.append(self._extract_function_info(item))
        
        return ClassInfo(
            name=node.name,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            docstring=self._get_docstring(node),
            methods=methods,
        )

    def _extract_imports(self, tree: ast.AST) -> List[ImportInfo]:
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(ImportInfo(
                        module=alias.name,
                        names=[alias.name],
                        is_from=False,
                        alias=alias.asname,
                    ))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [alias.name for alias in node.names]
                for alias in node.names:
                    imports.append(ImportInfo(
                        module=module,
                        names=names,
                        is_from=True,
                        alias=alias.asname,
                    ))
        return imports

    def _extract_dependencies(self, tree: ast.AST, imports: List[ImportInfo]) -> Set[str]:
        dependencies = set()
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                for imp in imports:
                    if imp.names:
                        for name in imp.names:
                            if node.id == name or node.id == imp.alias:
                                if imp.module:
                                    dependencies.add(imp.module)
        
        return dependencies

    def analyze_file(self, file_path: str) -> dict:
        try:
            path = Path(file_path)
            if not path.exists():
                raise CodeAnalysisError(f"File not found: {file_path}")
            
            if path.suffix != ".py":
                raise CodeAnalysisError("Only Python files are supported")
            
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            
            tree = ast.parse(content, filename=str(path))
            
            functions = []
            classes = []
            imports = self._extract_imports(tree)
            
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.FunctionDef):
                    functions.append(self._extract_function_info(node))
                elif isinstance(node, ast.AsyncFunctionDef):
                    functions.append(self._extract_function_info(node))
                elif isinstance(node, ast.ClassDef):
                    classes.append(self._extract_class_info(node))
            
            dependencies = self._extract_dependencies(tree, imports)
            
            return {
                "success": True,
                "file_path": str(path),
                "functions": [f.to_dict() for f in functions],
                "classes": [c.to_dict() for c in classes],
                "imports": [i.to_dict() for i in imports],
                "dependencies": list(dependencies),
                "total_lines": len(content.split("\n")),
            }
            
        except SyntaxError as e:
            self.logger.error("syntax_error", file=file_path, error=str(e))
            return {
                "success": False,
                "error": f"Syntax error: {e.msg} at line {e.lineno}",
            }
        except Exception as e:
            self.logger.error("analysis_error", file=file_path, error=str(e))
            return {"success": False, "error": str(e)}

    def analyze_directory(self, dir_path: str, pattern: str = "**/*.py") -> dict:
        try:
            path = Path(dir_path)
            if not path.is_dir():
                raise CodeAnalysisError(f"Not a directory: {dir_path}")
            
            files = list(path.glob(pattern))
            results = []
            
            for file_path in files:
                result = self.analyze_file(str(file_path))
                results.append(result)
            
            total_functions = sum(
                1 for r in results if r.get("success") for f in r.get("functions", [])
            )
            total_classes = sum(
                1 for r in results if r.get("success") for c in r.get("classes", [])
            )
            
            return {
                "success": True,
                "analyzed_files": len(results),
                "files": results,
                "summary": {
                    "total_functions": total_functions,
                    "total_classes": total_classes,
                },
            }
            
        except Exception as e:
            self.logger.error("directory_analysis_error", dir=dir_path, error=str(e))
            return {"success": False, "error": str(e)}

    def get_function_at_line(self, file_path: str, line: int) -> Optional[FunctionInfo]:
        result = self.analyze_file(file_path)
        if not result.get("success"):
            return None
        
        for func in result.get("functions", []):
            if func["line_start"] <= line <= func["line_end"]:
                return FunctionInfo(**func)
        
        for cls in result.get("classes", []):
            for method in cls.get("methods", []):
                if method["line_start"] <= line <= method["line_end"]:
                    return FunctionInfo(**method)
        
        return None

    def find_function(self, file_path: str, function_name: str) -> Optional[FunctionInfo]:
        result = self.analyze_file(file_path)
        if not result.get("success"):
            return None
        
        for func in result.get("functions", []):
            if func["name"] == function_name:
                return FunctionInfo(**func)
        
        for cls in result.get("classes", []):
            for method in cls.get("methods", []):
                if method["name"] == function_name:
                    return FunctionInfo(**method)
        
        return None