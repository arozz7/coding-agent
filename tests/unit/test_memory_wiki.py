"""Unit tests for memory wiki."""
import pytest
from agent.memory.memory_wiki import MemoryWiki, create_memory_wiki


class TestMemoryWiki:
    def test_initialization(self):
        wiki = MemoryWiki("test_project")
        
        assert wiki.project_id == "test_project"
        assert wiki.graph is not None

    def test_add_file(self):
        wiki = MemoryWiki()
        
        wiki.add_file("test.py", file_type="source", language="python")
        
        assert "test.py" in wiki._file_nodes
        assert wiki.graph.has_node("test.py")

    def test_add_duplicate_file(self):
        wiki = MemoryWiki()
        
        wiki.add_file("test.py")
        wiki.add_file("test.py")  # Should not error
        
        assert len(wiki._file_nodes) == 1

    def test_add_function(self):
        wiki = MemoryWiki()
        
        wiki.add_function(
            "test.py",
            "my_function",
            "def my_function(arg1, arg2)",
            10,
            15,
            calls=["other_function"],
        )
        
        node_id = "test.py:my_function"
        assert node_id in wiki._function_nodes
        assert wiki.graph.has_edge("test.py", node_id)

    def test_add_class(self):
        wiki = MemoryWiki()
        
        wiki.add_class(
            "test.py",
            "MyClass",
            1,
            30,
            methods=["method1", "method2"],
            base_classes=["BaseClass"],
        )
        
        node_id = "test.py:MyClass"
        assert node_id in wiki._class_nodes

    def test_add_import(self):
        wiki = MemoryWiki()
        
        wiki.add_file("main.py")
        wiki.add_import("main.py", "os", ["path"])
        
        assert len(wiki.get_file_imports("main.py")) == 1

    def test_get_dependencies(self):
        wiki = MemoryWiki()
        
        wiki.add_file("a.py")
        wiki.add_file("b.py")
        wiki.add_function("a.py", "func_a", "def func_a()", 1, 5, calls=["b.py:func_b"])
        
        deps = wiki.get_dependencies("a.py", max_depth=1)
        assert len(deps) > 0

    def test_get_dependents(self):
        wiki = MemoryWiki()
        
        wiki.add_file("util.py")
        wiki.add_file("main.py")
        
        dependents = wiki.get_dependents("util.py")
        assert isinstance(dependents, list)

    def test_find_function_call_chain(self):
        wiki = MemoryWiki()
        
        wiki.add_function("a.py", "func_a", "def func_a()", 1, 5)
        wiki.add_function("b.py", "func_b", "def func_b()", 1, 5, calls=["a.py:func_a"])
        
        chain = wiki.find_function_call_chain("b.py:func_b", "a.py:func_a")
        
        assert chain is not None
        assert len(chain) >= 2

    def test_find_function_call_chain_no_path(self):
        wiki = MemoryWiki()
        
        wiki.add_function("a.py", "func_a", "def func_a()", 1, 5)
        wiki.add_function("b.py", "func_b", "def func_b()", 1, 5)
        
        chain = wiki.find_function_call_chain("a.py:func_a", "b.py:func_b")
        
        assert chain is None

    def test_get_file_imports(self):
        wiki = MemoryWiki()
        
        wiki.add_file("main.py")
        wiki.add_import("main.py", "os", ["path", "listdir"])
        wiki.add_import("main.py", "sys", ["argv"])
        
        imports = wiki.get_file_imports("main.py")
        
        assert len(imports) == 2

    def test_get_file_functions(self):
        wiki = MemoryWiki()
        
        wiki.add_file("test.py")
        wiki.add_function("test.py", "func1", "def func1():", 1, 5)
        wiki.add_function("test.py", "func2", "def func2():", 10, 15)
        
        functions = wiki.get_file_functions("test.py")
        
        assert len(functions) == 2

    def test_get_file_classes(self):
        wiki = MemoryWiki()
        
        wiki.add_file("test.py")
        wiki.add_class("test.py", "MyClass", 1, 20, methods=["do_something"])
        
        classes = wiki.get_file_classes("test.py")
        
        assert len(classes) == 1
        assert classes[0]["name"] == "MyClass"

    def test_get_impact_analysis(self):
        wiki = MemoryWiki()
        
        wiki.add_file("core.py")
        wiki.add_file("app.py")
        wiki.add_file("util.py")
        
        impact = wiki.get_impact_analysis("core.py")
        
        assert "file" in impact
        assert "directly_imports" in impact
        assert "risk_level" in impact

    def test_get_statistics(self):
        wiki = MemoryWiki()
        
        wiki.add_file("test.py")
        wiki.add_function("test.py", "func1", "def func1()", 1, 5)
        wiki.add_class("test.py", "MyClass", 10, 20)
        
        stats = wiki.get_statistics()
        
        assert stats["files"] == 1
        assert stats["functions"] == 1
        assert stats["classes"] == 1

    def test_export_to_dict(self):
        wiki = MemoryWiki("export_test")
        
        wiki.add_file("test.py")
        wiki.add_function("test.py", "func1", "def func1()", 1, 5)
        
        exported = wiki.export_to_dict()
        
        assert exported["project_id"] == "export_test"
        assert "nodes" in exported
        assert "edges" in exported

    def test_clear(self):
        wiki = MemoryWiki()
        
        wiki.add_file("test.py")
        wiki.add_function("test.py", "func1", "def func1()", 1, 5)
        
        assert wiki.graph.number_of_nodes() > 0
        
        wiki.clear()
        
        assert wiki.graph.number_of_nodes() == 0


class TestCreateMemoryWiki:
    def test_create_default(self):
        wiki = create_memory_wiki()
        
        assert wiki.project_id == "default"

    def test_create_custom_project(self):
        wiki = create_memory_wiki("my_project")
        
        assert wiki.project_id == "my_project"