import os
import platform
import shutil
import subprocess
from typing import Optional, List
import structlog

logger = structlog.get_logger()


class PlatformUtils:
    @staticmethod
    def get_os() -> str:
        return platform.system().lower()
    
    @staticmethod
    def is_windows() -> bool:
        return platform.system() == "Windows"
    
    @staticmethod
    def is_macos() -> bool:
        return platform.system() == "Darwin"
    
    @staticmethod
    def is_linux() -> bool:
        return platform.system() == "Linux"
    
    @staticmethod
    def get_home_dir() -> str:
        return str(os.path.expanduser("~"))
    
    @staticmethod
    def get_path_separator() -> str:
        return os.sep
    
    @staticmethod
    def normalize_path(path: str) -> str:
        return os.path.normpath(path)
    
    @staticmethod
    def get_temp_dir() -> str:
        return os.path.normpath(os.path.realpath(tempfile.gettempdir()))


class ShellExecutor:
    def __init__(self, working_dir: Optional[str] = None):
        self.working_dir = working_dir
        self.logger = logger.bind(component="shell_executor")
        self._shell = self._detect_shell()
        self._is_windows = platform.system() == "Windows"
    
    def _detect_shell(self) -> str:
        system = platform.system()
        if system == "Windows":
            return shutil.which("pwsh") or "powershell"
        return shutil.which("bash") or "sh"
    
    def is_windows(self) -> bool:
        return self._is_windows
    
    def get_shell(self) -> str:
        return self._shell
    
    def _build_env(self, env_vars: Optional[dict] = None) -> dict:
        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)
        return env
    
    def run(
        self,
        command: str,
        timeout: int = 30,
        env_vars: Optional[dict] = None,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        shell = self._shell
        
        if self.is_windows() and shell in ("powershell", "pwsh"):
            command = command.replace("&&", ";")
        
        self.logger.debug("executing_command", command=command, shell=shell, cwd=self.working_dir)
        
        result = subprocess.run(
            command,
            shell=True,
            cwd=self.working_dir,
            env=self._build_env(env_vars),
            capture_output=capture_output,
            text=True,
            timeout=timeout,
        )
        return result
    
    async def run_async(
        self,
        command: str,
        timeout: int = 30,
        env_vars: Optional[dict] = None,
    ) -> subprocess.CompletedProcess:
        import asyncio
        
        shell = self._shell
        
        if self.is_windows() and shell in ("powershell", "pwsh"):
            command = command.replace("&&", ";")
        
        self.logger.debug("executing_command_async", command=command, shell=shell)
        
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_dir,
            env=self._build_env(env_vars),
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        
        return subprocess.CompletedProcess(
            args=command,
            returncode=proc.returncode,
            stdout=stdout.decode() if stdout else "",
            stderr=stderr.decode() if stderr else "",
        )


import tempfile


def get_default_shell() -> str:
    return ShellExecutor().get_shell()


def get_platform_info() -> dict:
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "python_version": platform.python_version(),
        "architecture": platform.machine(),
        "shell": get_default_shell(),
        "home_dir": PlatformUtils.get_home_dir(),
        "temp_dir": PlatformUtils.get_temp_dir(),
    }