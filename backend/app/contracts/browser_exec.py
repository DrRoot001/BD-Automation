"""Protocol definitions for Module 4 (Browser Automation)."""

from typing import Protocol, Dict, Any

class BrowserExecutionContract(Protocol):
    async def execute_application(self, application_package: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a job application autonomously in the browser."""
        ...
        
    async def verify_login(self, platform: str, credentials: Dict[str, str]) -> bool:
        """Verify login credentials for a specific ATS/platform."""
        ...
