import os
import requests
import asyncio
from discord import Client, Intents, Message, File
from discord.ext import commands

API_URL = os.getenv("AGENT_API_URL", "http://localhost:5005")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "900"))

# Create a persistent session for connection reuse
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,
    pool_maxsize=10,
    max_retries=3
)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)


class AgentClient:
    def __init__(self, api_url: str = API_URL):
        self.api_url = api_url
        print(f"AgentClient initialized with API URL: {api_url}, timeout: {REQUEST_TIMEOUT}s")
    
    def send_task(self, task: str, session_id: str = None) -> dict:
        payload = {"task": task}
        if session_id:
            payload["session_id"] = session_id
        
        print(f"Sending request to {self.api_url}/task...")
        
        try:
            response = _session.post(
                f"{self.api_url}/task", 
                json=payload, 
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            result = response.json()
            print(f"Got response: success={result.get('success')}")
            return result
        except requests.exceptions.Timeout:
            print(f"TIMEOUT: Request timed out after {REQUEST_TIMEOUT}s")
            return {"success": False, "error": f"Request timed out ({REQUEST_TIMEOUT}s) - local model may be slow. Try restarting the API."}
        except requests.exceptions.ConnectionError as e:
            print(f"CONNECTION ERROR: {e}")
            return {"success": False, "error": f"Connection failed. Is the API server running?"}
        except requests.exceptions.RequestException as e:
            print(f"REQUEST ERROR: {e}")
            return {"success": False, "error": str(e)}
    
    def get_session_history(self, session_id: str) -> dict:
        try:
            response = _session.get(f"{self.api_url}/sessions/{session_id}", timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def list_sessions(self) -> dict:
        try:
            response = _session.get(f"{self.api_url}/sessions", timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def delete_session(self, session_id: str) -> dict:
        try:
            response = _session.delete(f"{self.api_url}/sessions/{session_id}", timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}


class DiscordAgentBot(commands.Bot):
    def __init__(self):
        intents = Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.agent_client = AgentClient()
        self.user_sessions = {}
    
    async def on_ready(self):
        print(f"Logged in as {self.user}")
        print(f"API URL: {API_URL}")
    
    async def on_message(self, message: Message):
        if message.author == self.user:
            return
        
        if not message.content.startswith("!"):
            return
        
        await self.process_commands(message)


bot = DiscordAgentBot()


@bot.command(name="ask")
async def ask(ctx, *, question: str):
    """Ask the agent a question"""
    await ctx.send("🤔 Thinking...")
    
    session_id = str(ctx.author.id)
    
    try:
        result = bot.agent_client.send_task(question, session_id)
        
        if result.get("success"):
            response = result.get("response", "No response")
            
            if not response:
                await ctx.send("⚠️ Empty response received")
                return
            
            # Split by newlines to avoid Discord formatting issues, then send in chunks
            lines = response.split('\n')
            current_chunk = []
            current_length = 0
            
            for line in lines:
                line_length = len(line) + 1  # +1 for newline
                if current_length + line_length > 1800 and current_chunk:
                    await ctx.send('\n'.join(current_chunk))
                    current_chunk = []
                    current_length = 0
                current_chunk.append(line)
                current_length += line_length
            
            # Send remaining
            if current_chunk:
                await ctx.send('\n'.join(current_chunk))
        else:
            error = result.get("error", "Unknown error")
            await ctx.send(f"❌ Error: {error}")
            
    except Exception as e:
        await ctx.send(f"❌ Exception: {str(e)}")


@bot.command(name="history")
async def history(ctx):
    """Show conversation history for this user"""
    session_id = str(ctx.author.id)
    result = bot.agent_client.get_session_history(session_id)
    
    if "error" in result:
        await ctx.send(f"❌ Error: {result['error']}")
        return
    
    history = result.get("history", [])
    
    if not history:
        await ctx.send("No conversation history found.")
        return
    
    await ctx.send(f"📋 Found {len(history)} messages:")
    
    for msg in history[-5:]:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")[:100]
        await ctx.send(f"**{role}**: {content}...")


@bot.command(name="sessions")
async def list_sessions(ctx):
    """List all sessions"""
    result = bot.agent_client.list_sessions()
    
    if "error" in result:
        await ctx.send(f"❌ Error: {result['error']}")
        return
    
    sessions = result.get("sessions", [])
    
    if not sessions:
        await ctx.send("No sessions found.")
        return
    
    await ctx.send(f"📋 Found {len(sessions)} sessions:")
    for s in sessions[:5]:
        sid = s.get("session_id", "unknown")
        count = s.get("message_count", 0)
        await ctx.send(f"  - {sid}: {count} messages")


@bot.command(name="helpme")
async def help_command(ctx):
    """Show available commands"""
    help_text = """
🤖 **Agent Commands**

**Core:**
- `!ask <question>` - Ask the coding agent anything

**Code:**
- `!explain <code>` - Explain what code does
- `!test <code>` - Generate tests for code
- `!refactor <code>` - Refactor/improve code
- `!review` - Review last shared code

**Git:**
- `!git <command>` - Run git (status, log, diff, branch)

**Docs & Info:**
- `!docs <topic>` - Get documentation on a topic
- `!session` - Show current session info

**Workspace:**
- `!workspace` - Show current workspace & contents
- `!cd <path>` - Change workspace directory

**Screenshots:**
- `!screenshot [url]` - Take screenshot of localhost:8080 or custom URL

**History:**
- `!history` - Show conversation history
- `!clear` - Clear your conversation
- `!sessions` - List all sessions
- `!helpme` - Show this help
"""
    await ctx.send(help_text)


@bot.command(name="explain")
async def explain_code(ctx, *, code: str):
    """Explain what code does"""
    await ctx.send("🤔 Analyzing code...")
    
    session_id = str(ctx.author.id)
    task = f"Explain this code in simple terms:\n```{code}```"
    
    result = bot.agent_client.send_task(task, session_id)
    
    if result.get("success"):
        response = result.get("response", "No response")
        await ctx.send(response[:2000] if len(response) > 2000 else response)
    else:
        await ctx.send(f"❌ Error: {result.get('error', 'Unknown')}")


@bot.command(name="test")
async def generate_tests(ctx, *, code: str):
    """Generate tests for code"""
    await ctx.send("🧪 Generating tests...")
    
    session_id = str(ctx.author.id)
    task = f"Generate unit tests (pytest) for this code:\n```{code}```\n\nWrite only the test code, no explanations."
    
    result = bot.agent_client.send_task(task, session_id)
    
    if result.get("success"):
        response = result.get("response", "No response")
        await ctx.send(f"```python\n{response[:1800]}\n```")
    else:
        await ctx.send(f"❌ Error: {result.get('error', 'Unknown')}")


@bot.command(name="refactor")
async def refactor_code(ctx, *, code: str):
    """Refactor/improve code"""
    await ctx.send("🔧 Refactoring code...")
    
    session_id = str(ctx.author.id)
    task = f"Refactor and improve this code. Keep it in the same language:\n```{code}```\n\nReturn only the improved code with brief explanation."
    
    result = bot.agent_client.send_task(task, session_id)
    
    if result.get("success"):
        response = result.get("response", "No response")
        await ctx.send(response[:2000])
    else:
        await ctx.send(f"❌ Error: {result.get('error', 'Unknown')}")


@bot.command(name="review")
async def review_code(ctx):
    """Review last shared code"""
    await ctx.send("🔍 Looking for recent code...")
    
    session_id = str(ctx.author.id)
    history = bot.agent_client.get_session_history(session_id)
    
    code_msg = None
    if "history" in history:
        for msg in reversed(history["history"][-5:]):
            content = msg.get("content", "")
            if "```" in content:
                code_msg = content
                break
    
    if not code_msg:
        await ctx.send("No code found in recent conversation. Use `!explain <code>` or `!test <code>` first.")
        return
    
    session_id = str(ctx.author.id)
    task = f"Review this code for issues, improvements, and best practices:\n{code_msg}"
    
    result = bot.agent_client.send_task(task, session_id)
    
    if result.get("success"):
        response = result.get("response", "No response")
        await ctx.send(response[:2000])
    else:
        await ctx.send(f"❌ Error: {result.get('error', 'Unknown')}")


@bot.command(name="git")
async def git_command(ctx, *, args: str):
    """Run git commands"""
    import subprocess
    
    valid_cmds = ["status", "log", "diff", "branch", "log --oneline -5", "status --porcelain"]
    
    if args.strip() not in valid_cmds and not any(args.strip().startswith(v.split()[0]) for v in valid_cmds):
        await ctx.send(f"❌ Only these commands allowed: {', '.join(valid_cmds)}")
        return
    
    try:
        result = subprocess.run(f"git {args}", capture_output=True, text=True, shell=True)
        output = result.stdout or result.stderr
        
        if not output:
            output = "No output"
            
        await ctx.send(f"```\n{output[:1800]}\n```")
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")


@bot.command(name="docs")
async def get_docs(ctx, *, topic: str):
    """Get documentation on a topic"""
    await ctx.send("📚 Fetching docs...")
    
    session_id = str(ctx.author.id)
    task = f"Give me a concise summary and key points about: {topic}\n\nInclude code examples if relevant."
    
    result = bot.agent_client.send_task(task, session_id)
    
    if result.get("success"):
        response = result.get("response", "No response")
        await ctx.send(response[:2000])
    else:
        await ctx.send(f"❌ Error: {result.get('error', 'Unknown')}")


@bot.command(name="session")
async def show_session(ctx):
    """Show current session info"""
    session_id = str(ctx.author.id)
    history = bot.agent_client.get_session_history(session_id)
    
    if "error" in history:
        await ctx.send(f"❌ Error: {history['error']}")
        return
    
    msg_count = len(history.get("history", []))
    
    await ctx.send(f"📋 **Session Info**\n- Session ID: `{session_id}`\n- Messages: {msg_count}")


@bot.command(name="load")
async def load_session(ctx, *, session_id: str):
    """Load a specific session by ID"""
    history = bot.agent_client.get_session_history(session_id)
    
    if "error" in history:
        await ctx.send(f"❌ Error: {history['error']}")
        return
    
    messages = history.get("history", [])
    
    if not messages:
        await ctx.send(f"⚠️ No messages found in session: {session_id}")
        return
    
    await ctx.send(f"✅ Loaded session: `{session_id}` ({len(messages)} messages)")
    
    # Show last few messages
    for msg in messages[-3:]:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")[:150]
        await ctx.send(f"**{role}**: {content}...")


@bot.command(name="clear")
async def clear_history(ctx):
    """Clear conversation history"""
    session_id = str(ctx.author.id)
    
    try:
        result = bot.agent_client.delete_session(session_id)
        
        if result.get("success") or "error" not in result:
            await ctx.send("✅ Conversation history cleared!")
        else:
            await ctx.send(f"❌ Error: {result.get('error', 'Unknown')}")
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")


@bot.command(name="workspace")
async def show_workspace(ctx):
    """Show current workspace"""
    try:
        response = _session.get(f"{bot.agent_client.api_url}/workspace", timeout=10)
        data = response.json()
        
        await ctx.send(f"📁 **Current Workspace:**\n{data.get('workspace', 'Unknown')}")
        
        # Also show contents
        dirs_response = _session.get(f"{bot.agent_client.api_url}/workspace/directories", timeout=10)
        dirs_data = dirs_response.json()
        
        if "items" in dirs_data:
            items = dirs_data["items"]
            if items:
                msg = "**Contents:**\n"
                for item in items[:10]:
                    emoji = "📁" if item["type"] == "directory" else "📄"
                    msg += f"{emoji} {item['name']}\n"
                await ctx.send(msg)
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")


@bot.command(name="cd")
async def change_workspace(ctx, *, path: str):
    """Change workspace directory"""
    try:
        response = _session.post(
            f"{bot.agent_client.api_url}/workspace",
            json={"path": path},
            timeout=120
        )
        data = response.json()
        
        if response.status_code == 200:
            await ctx.send(f"✅ Workspace changed to: {data.get('workspace')}")
        else:
            await ctx.send(f"❌ Error: {data.get('detail', 'Unknown error')}")
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")


@bot.command(name="screenshot")
async def take_screenshot(ctx, url: str = "http://localhost:8080"):
    """Take a screenshot of a URL and upload to Discord"""
    await ctx.send("📸 Taking screenshot...")
    
    try:
        response = _session.post(
            f"{bot.agent_client.api_url}/screenshot",
            json={"url": url},
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success"):
                path = result.get("path")
                await ctx.send(f"✅ Screenshot saved to: {path}")
                # Try to upload the file to Discord
                try:
                    import os
                    if os.path.exists(path):
                        await ctx.send(file=discord.File(path))
                    else:
                        await ctx.send(f"⚠️ File not found at: {path}")
                except Exception as e:
                    await ctx.send(f"⚠️ Could not upload file: {str(e)}")
            else:
                await ctx.send(f"❌ Screenshot failed: {result.get('error', 'Unknown')}")
        else:
            await ctx.send(f"❌ Error: {response.text}")
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")


def run_bot(token: str):
    if not token:
        print("ERROR: Discord bot token not set. Set DISCORD_BOT_TOKEN environment variable.")
        return
    
    print(f"Starting Discord bot, connecting to API at {API_URL}")
    bot.run(token)


if __name__ == "__main__":
    import os
    token = os.getenv("DISCORD_BOT_TOKEN")
    run_bot(token)