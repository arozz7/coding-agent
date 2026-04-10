"""Unit tests for platform utilities."""
import pytest
import platform
import os


class TestPlatformUtils:
    def test_get_os(self):
        from agent.platform import PlatformUtils
        
        os_name = PlatformUtils.get_os()
        assert os_name in ("windows", "darwin", "linux")

    def test_is_windows(self):
        from agent.platform import PlatformUtils
        
        is_windows = PlatformUtils.is_windows()
        assert is_windows == (platform.system() == "Windows")

    def test_is_macos(self):
        from agent.platform import PlatformUtils
        
        is_macos = PlatformUtils.is_macos()
        assert is_macos == (platform.system() == "Darwin")

    def test_is_linux(self):
        from agent.platform import PlatformUtils
        
        is_linux = PlatformUtils.is_linux()
        assert is_linux == (platform.system() == "Linux")

    def test_get_home_dir(self):
        from agent.platform import PlatformUtils
        
        home = PlatformUtils.get_home_dir()
        assert home == os.path.expanduser("~")

    def test_get_path_separator(self):
        from agent.platform import PlatformUtils
        
        sep = PlatformUtils.get_path_separator()
        assert sep == os.sep

    def test_normalize_path(self):
        from agent.platform import PlatformUtils
        
        path = "path/to/somewhere"
        normalized = PlatformUtils.normalize_path(path)
        assert normalized is not None


class TestShellExecutor:
    def test_initialization(self):
        from agent.platform import ShellExecutor
        
        executor = ShellExecutor()
        assert executor.get_shell() is not None

    def test_get_shell(self):
        from agent.platform import ShellExecutor
        
        executor = ShellExecutor()
        shell = executor.get_shell()
        
        if platform.system() == "Windows":
            assert shell in ("powershell", "pwsh", "cmd")
        else:
            assert shell in ("bash", "sh")

    def test_run_simple_command(self):
        from agent.platform import ShellExecutor
        
        executor = ShellExecutor()
        
        result = executor.run("echo test")
        
        assert result.returncode == 0
        assert "test" in result.stdout.lower()


class TestHelperFunctions:
    def test_get_default_shell(self):
        from agent.platform import get_default_shell
        
        shell = get_default_shell()
        assert shell is not None
        assert len(shell) > 0

    def test_get_platform_info(self):
        from agent.platform import get_platform_info
        
        info = get_platform_info()
        
        assert "os" in info
        assert "python_version" in info
        assert "shell" in info
        assert "home_dir" in info
        assert "temp_dir" in info